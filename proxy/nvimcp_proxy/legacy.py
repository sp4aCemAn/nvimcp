#!/usr/bin/env python3
import argparse
import json
import os
import selectors
import signal
import socket
import sys
import time

SERVER_NAME = "nvimcp"
SERVER_VERSION = "0.2.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
DEFAULT_TIMEOUT = 30.0
DEFAULT_WAIT = 10.0
SELECT_TICK = 0.5
CONNECT_RETRY = 0.5

RPC_REQUEST = 0
RPC_RESPONSE = 1
RPC_NOTIFICATION = 2

NVIMCP_RPC_LUA = "return require('nvimcp.server').rpc(...)"
HEALTH_LUA = "return require('nvimcp.server').socket_path() ~= nil"

JSON_DUMP_KW = {"ensure_ascii": False, "separators": (",", ":")}


from .errors import ProxyError
from .mp import Incomplete, mp_decode_one, mp_encode
from .discovery import SessionFinder

class NvimcpProxy:
    def __init__(self, args):
        self.finder = SessionFinder(
            args.socket, args.pid, args.sessions_file,
            getattr(args, "skip_ancestry", False), self._log, self._warn,
        )
        self.timeout = args.timeout
        self.wait = args.wait
        self.debug = args.debug
        self.sel = selectors.DefaultSelector()
        self.sock = None
        self.connected = False
        self.nvim_rx = bytearray()
        self.stdin_rx = bytearray()
        self.pending = {}
        self.parked = []
        self.connect_started = None
        self.next_connect_retry = 0.0
        self.last_connect_err = None
        self.next_msgid = 1
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
                        self._on_stdin()
                    elif key.data == "nvim":
                        self._on_nvim_data()
                if self.exit_code is None:
                    self._pump_connect()
                    self._check_timeouts()
        finally:
            self._close_nvim()
            self.sel.close()
        return self.exit_code if self.exit_code is not None else 0

    def _shutdown(self, code):
        self.exit_code = code

    def _flush_parked_on_eof(self):
        """stdin closed while waiting for a session: one last discovery try
        via the normal pump, then report and go."""
        self._pump_connect()
        if self.parked and self.sock is None:
            err = self.last_connect_err or "no nvimcp session found"
            for mcp_id, _what, _params in self.parked:
                self._send_mcp_error(mcp_id, -32603, err)
            self.parked = []

    def _select_timeout(self):
        timeout = SELECT_TICK
        if self.parked and self.sock is None and self.connect_started is not None:
            now = time.monotonic()
            deadline = self.connect_started + self.wait
            timeout = min(timeout, max(0.0, deadline - now), CONNECT_RETRY)
        if not self.pending:
            return timeout
        now = time.monotonic()
        nearest = min(m["deadline"] for m in self.pending.values())
        return max(0.0, min(nearest - now, timeout))

    def _check_timeouts(self):
        now = time.monotonic()
        for msgid in list(self.pending):
            meta = self.pending[msgid]
            if meta["deadline"] > now:
                continue
            if meta["kind"] == "health":
                self._nvim_died("nvim health check timed out")
                return
            del self.pending[msgid]
            self._send_mcp_error(
                meta["mcp_id"], -32603, "nvim rpc timed out after %.0fs (%s)" % (self.timeout, meta["what"])
            )

    def _on_stdin(self):
        try:
            chunk = os.read(0, 65536)
        except OSError:
            self._flush_parked_on_eof()
            self._shutdown(0)
            return
        if not chunk:
            self._flush_parked_on_eof()
            self._shutdown(0)
            return
        self.stdin_rx += chunk
        while True:
            i = self.stdin_rx.find(b"\n")
            if i < 0:
                break
            line = bytes(self.stdin_rx[:i])
            del self.stdin_rx[: i + 1]
            self._on_mcp_line(line)

    def _on_mcp_line(self, line):
        text = line.strip()
        if not text:
            return
        try:
            msg = json.loads(text)
        except ValueError as e:
            self._send_mcp_error(None, -32700, "parse error: %s" % e)
            return
        if isinstance(msg, dict) and isinstance(msg.get("method"), str):
            self._on_mcp_msg(msg)

    def _on_mcp_msg(self, msg):
        mcp_id = msg.get("id")
        if mcp_id is None:
            self._log("notification %s" % msg.get("method"))
            return
        method = msg["method"]
        params = msg.get("params")
        if not isinstance(params, dict):
            params = {}
        if method == "initialize":
            pv = params.get("protocolVersion")
            self._send_mcp_result(mcp_id, {
                "protocolVersion": pv if isinstance(pv, str) and pv else DEFAULT_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": "Live Neovim buffers and LSP state via nvimcp tools.",
            })
        elif method == "ping":
            self._send_mcp_result(mcp_id, {})
        elif method == "tools/list":
            self._needs_nvim(mcp_id, "list_tools", {})
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments")
            if not isinstance(name, str) or not name:
                self._send_mcp_error(mcp_id, -32602, "tools/call requires params.name")
            elif arguments is not None and not isinstance(arguments, dict):
                self._send_mcp_error(mcp_id, -32602, "params.arguments must be an object")
            else:
                self._needs_nvim(mcp_id, "call_tool", {"name": name, "arguments": arguments or {}})
        elif method == "prompts/list":
            self._send_mcp_result(mcp_id, {"prompts": []})
        elif method == "resources/list":
            self._send_mcp_result(mcp_id, {"resources": []})
        elif method == "resources/templates/list":
            self._send_mcp_result(mcp_id, {"resourceTemplates": []})
        elif method == "logging/setLevel":
            self._send_mcp_result(mcp_id, {})
        else:
            self._send_mcp_error(mcp_id, -32601, "method not found: %s" % method)

    def _needs_nvim(self, mcp_id, what, params):
        try:
            if not self.connected:
                self._connect()
                self._park(mcp_id, what, params)
                return
            self._send_nvimcp_rpc(mcp_id, what, params)
        except ProxyError as e:
            if self.wait > 0:
                # No session (yet): park and retry discovery within the wait
                # window so a client may start before nvim does.
                self._park(mcp_id, what, params)
                self.last_connect_err = str(e)
                if self.connect_started is None:
                    self.connect_started = time.monotonic()
            else:
                self._send_mcp_error(mcp_id, -32603, str(e))

    def _park(self, mcp_id, what, params):
        if not any(pe[0] == mcp_id for pe in self.parked):
            self.parked.append((mcp_id, what, params))

    def _pump_connect(self):
        """Retry discovery while parked requests are older than the wait
        window; report errors to their clients when the window expires."""
        if self.sock is not None or not self.parked:
            return
        now = time.monotonic()
        if self.connect_started is not None and now - self.connect_started > self.wait:
            err = self.last_connect_err or "no nvimcp session found"
            for mcp_id, _what, _params in self.parked:
                self._send_mcp_error(
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
        if self.sock is not None:
            return
        target = self.finder.discover()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3.0)
        try:
            s.connect(target["socket"])
        except OSError as e:
            s.close()
            raise ProxyError(
                "cannot connect to nvim socket %s: %s (is the nvim session still alive?)"
                % (target["socket"], e)
            )
        s.setblocking(False)
        self.sock = s
        self.nvim_rx = bytearray()
        self.sel.register(s, selectors.EVENT_READ, "nvim")
        self._nvim_request("nvim_exec_lua", [HEALTH_LUA, []], {"kind": "health", "pid": target.get("pid")})

    def _send_nvimcp_rpc(self, mcp_id, what, params):
        self._nvim_request(
            "nvim_exec_lua",
            [NVIMCP_RPC_LUA, [what, params]],
            {"kind": "mcp", "mcp_id": mcp_id, "what": what},
        )

    def _nvim_request(self, method, params, meta):
        msgid = self.next_msgid
        self.next_msgid += 1
        meta["deadline"] = time.monotonic() + self.timeout
        self.pending[msgid] = meta
        try:
            self.sock.sendall(mp_encode([RPC_REQUEST, msgid, method, params]))
        except OSError as e:
            self._nvim_died("nvim socket write failed: %s" % e)

    def _on_nvim_data(self):
        try:
            chunk = self.sock.recv(65536)
        except BlockingIOError:
            return
        except OSError as e:
            self._nvim_died("nvim socket error: %s" % e)
            return
        if not chunk:
            self._nvim_died("nvim session disconnected")
            return
        self.nvim_rx += chunk
        while True:
            try:
                value, off = mp_decode_one(self.nvim_rx, 0)
            except Incomplete:
                return
            except ValueError as e:
                self._nvim_died("msgpack decode error: %s" % e)
                return
            del self.nvim_rx[:off]
            self._handle_nvim(value)

    def _handle_nvim(self, value):
        if not isinstance(value, list) or len(value) < 4 or value[0] != RPC_RESPONSE:
            return
        msgid, err, result = value[1], value[2], value[3]
        meta = self.pending.pop(msgid, None)
        if meta is None:
            return
        if err is not None:
            text = self._nvim_err_text(err)
            if meta["kind"] == "health":
                self._nvim_died("nvim rejected the nvimcp health check: %s%s" % (text, self._plugin_hint(text)))
            else:
                self._send_mcp_error(meta["mcp_id"], -32603, "nvim rpc failed: %s" % text)
            return
        if meta["kind"] == "health":
            if result is True:
                self.connected = True
                self._log("connected to nvim pid %s" % meta.get("pid"))
                parked, self.parked = self.parked, []
                for mcp_id, what, params in parked:
                    self._send_nvimcp_rpc(mcp_id, what, params)
            else:
                self._nvim_died("nvim has nvimcp loaded but the server is not started (run :NvimcpStart in nvim)")
            return
        self._mcp_result_from(meta, result)

    def _mcp_result_from(self, meta, result):
        what = meta["what"]
        if not isinstance(result, dict):
            self._send_mcp_error(meta["mcp_id"], -32603, "nvimcp returned an unexpected response for %s" % what)
            return
        if result.get("error") is not None:
            if what == "call_tool":
                self._send_mcp_result(meta["mcp_id"], {
                    "content": [{"type": "text", "text": str(result["error"])}],
                    "isError": True,
                })
            else:
                self._send_mcp_error(meta["mcp_id"], -32603, str(result["error"]))
            return
        if what == "list_tools":
            tools = result.get("tools", [])
            self._send_mcp_result(meta["mcp_id"], {"tools": tools if isinstance(tools, list) else []})
        else:
            self._send_mcp_result(meta["mcp_id"], {
                "content": [{"type": "text", "text": json.dumps(result.get("result"), **JSON_DUMP_KW)}]
            })

    def _nvim_err_text(self, err):
        if isinstance(err, dict):
            msg = err.get("message")
            return str(msg) if msg else json.dumps(err, **JSON_DUMP_KW)
        return str(err)

    def _plugin_hint(self, text):
        if "nvimcp" in text and "not found" in text:
            return "; the nvim session does not have the nvimcp plugin loaded"
        return ""

    def _nvim_died(self, reason):
        self._close_nvim()
        for msgid in list(self.pending):
            meta = self.pending.pop(msgid)
            if meta["kind"] == "mcp":
                self._send_mcp_error(meta["mcp_id"], -32603, reason)
        for mcp_id, _what, _params in self.parked:
            self._send_mcp_error(mcp_id, -32603, reason)
        self.parked = []

    def _close_nvim(self):
        if self.sock is not None:
            try:
                self.sel.unregister(self.sock)
            except (KeyError, ValueError):
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        self.connected = False
        self.nvim_rx = bytearray()

    def _send_mcp_result(self, mcp_id, result):
        self._send({"jsonrpc": "2.0", "id": mcp_id, "result": result})

    def _send_mcp_error(self, mcp_id, code, message):
        self._send({"jsonrpc": "2.0", "id": mcp_id, "error": {"code": code, "message": message}})

    def _send(self, msg):
        try:
            sys.stdout.buffer.write((json.dumps(msg, **JSON_DUMP_KW) + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()
        except (BrokenPipeError, OSError):
            self._shutdown(1)


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
