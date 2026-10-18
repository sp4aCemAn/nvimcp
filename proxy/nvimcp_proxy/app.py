import argparse
import selectors
import signal
import sys
import time

from .bridge import NvimBridge
from .discovery import SessionFinder
from .errors import ProxyError
from .mcp import StdioEndpoint

DEFAULT_TIMEOUT = 30.0
DEFAULT_WAIT = 10.0
SELECT_TICK = 0.5
CONNECT_RETRY = 0.5


class NvimcpProxy:
    def __init__(self, args):
        self.finder = SessionFinder(
            args.socket, args.pid, args.sessions_file,
            getattr(args, "skip_ancestry", False), self._log, self._warn,
        )
        self.wait = args.wait
        self.debug = args.debug
        self.sel = selectors.DefaultSelector()
        self.mcp = StdioEndpoint(self._needs_nvim, self._on_stdin_eof, self._stdout_dead, self._log)
        self.bridge = NvimBridge(
            args.timeout, self._log, self.sel,
            self.mcp.send_error, self.mcp.result_from_nvimcp,
            self._nvim_died, self._nvim_ready,
        )
        self.parked = []
        self.connect_started = None
        self.next_connect_retry = 0.0
        self.last_connect_err = None
        self.exit_code = None
    def _log(self, msg):
        if self.debug:
            print("[nvimcp-proxy] %s" % msg, file=sys.stderr)

    def _warn(self, msg):
        print("[nvimcp-proxy] %s" % msg, file=sys.stderr)

    def run(self):
        self.sel.register(0, selectors.EVENT_READ, "stdin")
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: self._shutdown(0))
        try:
            while self.exit_code is None:
                events = self.sel.select(self._select_timeout())
                for key, _mask in events:
                    if key.data == "stdin":
                        self.mcp.poll()
                    elif key.data == "nvim":
                        self.bridge.poll()
                if self.exit_code is None:
                    self._pump_connect()
                    self.bridge.check_timeouts(time.monotonic())
        finally:
            self.bridge.close()
            self.sel.close()
        return self.exit_code if self.exit_code is not None else 0

    def _shutdown(self, code):
        self.exit_code = code

    def _on_stdin_eof(self):
        self._flush_parked_on_eof()
        self._shutdown(0)

    def _stdout_dead(self):
        self._shutdown(1)

    def _nvim_died(self, reason):
        for mcp_id, _what, _params in self.parked:
            self.mcp.send_error(mcp_id, -32603, reason)
        self.parked = []

    def _nvim_ready(self):
        parked, self.parked = self.parked, []
        for mcp_id, what, params in parked:
            self.bridge.send_tool(mcp_id, what, params)

    def _flush_parked_on_eof(self):
        """stdin closed while waiting for a session: one last discovery try
        via the normal pump, then report and go."""
        self._pump_connect()
        if self.parked and self.bridge.sock is None:
            err = self.last_connect_err or "no nvimcp session found"
            for mcp_id, _what, _params in self.parked:
                self.mcp.send_error(mcp_id, -32603, err)
            self.parked = []

    def _select_timeout(self):
        timeout = SELECT_TICK
        if self.parked and self.bridge.sock is None and self.connect_started is not None:
            now = time.monotonic()
            deadline = self.connect_started + self.wait
            timeout = min(timeout, max(0.0, deadline - now), CONNECT_RETRY)
        if not self.bridge.pending:
            return timeout
        now = time.monotonic()
        nearest = min(m["deadline"] for m in self.bridge.pending.values())
        return max(0.0, min(nearest - now, timeout))
    def _needs_nvim(self, mcp_id, what, params):
        try:
            if not self.bridge.connected:
                self._connect()
                self._park(mcp_id, what, params)
                return
            self.bridge.send_tool(mcp_id, what, params)
        except ProxyError as e:
            if self.wait > 0:
                # No session (yet): park and retry discovery within the wait
                # window so a client may start before nvim does.
                self._park(mcp_id, what, params)
                self.last_connect_err = str(e)
                if self.connect_started is None:
                    self.connect_started = time.monotonic()
            else:
                self.mcp.send_error(mcp_id, -32603, str(e))

    def _park(self, mcp_id, what, params):
        if not any(pe[0] == mcp_id for pe in self.parked):
            self.parked.append((mcp_id, what, params))

    def _pump_connect(self):
        """Retry discovery while parked requests are older than the wait
        window; report errors to their clients when the window expires."""
        if self.bridge.sock is not None or not self.parked:
            return
        now = time.monotonic()
        if self.connect_started is not None and now - self.connect_started > self.wait:
            err = self.last_connect_err or "no nvimcp session found"
            for mcp_id, _what, _params in self.parked:
                self.mcp.send_error(
                    mcp_id, -32603, "%s (waited %.0fs for an nvim session)" % (err, self.wait)
                )
            self.parked = []
            return
        if now >= self.next_connect_retry:
            self.next_connect_retry = now + CONNECT_RETRY
            try:
                self._connect()
            except ProxyError as e:
                self.last_connect_err = str(e)
    def _connect(self):
        target = self.finder.discover()
        self.bridge.connect(target)
def main():
    p = argparse.ArgumentParser(
        prog="nvimcp-proxy",
        description="Bridge an MCP client on stdio to a live Neovim session running nvimcp.",
        epilog=(
            "session selection: --socket wins; else --pid from the sessions file; else the session "
            "whose nvim cwd matches this process cwd; else the only session"
        ),
    )
    p.add_argument("--socket", help="connect to this nvimcp unix socket directly")
    p.add_argument("--pid", type=int, help="pick this nvim pid from the session registry")
    p.add_argument("--sessions-file", help="session registry file or directory (default: $XDG_RUNTIME_DIR/nvimcp; on macOS all <TMPDIR>/nvim.<user>/<random>/nvimcp candidates are scanned; reads legacy sessions.json and sessions/<pid>.json shards)")
    p.add_argument("--skip-ancestry", action="store_true", help="disable terminal-ancestry session matching (testing hook; ancestry matching is otherwise always on)")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="per-nvim-rpc timeout in seconds (default 30)")
    p.add_argument(
        "--wait",
        type=float,
        default=DEFAULT_WAIT,
        help="seconds to retry session discovery while requests are parked before failing; 0 disables (default 10)",
    )
    p.add_argument("--debug", action="store_true", help="log proxy activity to stderr")
    args = p.parse_args()
    sys.exit(NvimcpProxy(args).run())


if __name__ == "__main__":
    main()
