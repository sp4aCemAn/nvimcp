#!/usr/bin/env python3

import argparse
import json
import os
import selectors
import signal
import socket
import struct
import sys
import time

SERVER_NAME = "nvimcp"
SERVER_VERSION = "0.2.0"
DEFAULT_PROTOCOL_VERSION = "2024-11-05"
DEFAULT_TIMEOUT = 30.0
SELECT_TICK = 0.5

RPC_REQUEST = 0
RPC_RESPONSE = 1
RPC_NOTIFICATION = 2

NVIMCP_RPC_LUA = "return require('nvimcp.server').rpc(...)"
HEALTH_LUA = "return require('nvimcp.server').socket_path() ~= nil"

JSON_DUMP_KW = {"ensure_ascii": False, "separators": (",", ":")}


class Incomplete(Exception):
    pass


class ProxyError(Exception):
    pass


def mp_encode(value):
    out = bytearray()
    _enc(value, out)
    return bytes(out)


def _enc(v, out):
    if v is None:
        out.append(0xC0)
    elif v is True:
        out.append(0xC3)
    elif v is False:
        out.append(0xC2)
    elif isinstance(v, int):
        _enc_int(v, out)
    elif isinstance(v, float):
        out.append(0xCB)
        out += struct.pack(">d", v)
    elif isinstance(v, str):
        _enc_str(v, out)
    elif isinstance(v, list):
        _enc_array(v, out)
    elif isinstance(v, dict):
        _enc_dict(v, out)
    else:
        raise TypeError("cannot encode %s" % type(v).__name__)


def _enc_int(v, out):
    if v >= 0:
        if v < 0x80:
            out.append(v)
        elif v <= 0xFF:
            out.append(0xCC)
            out.append(v)
        elif v <= 0xFFFF:
            out.append(0xCD)
            out += struct.pack(">H", v)
        elif v <= 0xFFFFFFFF:
            out.append(0xCE)
            out += struct.pack(">I", v)
        else:
            out.append(0xCF)
            out += struct.pack(">Q", v)
    elif v >= -0x20:
        out.append(v & 0xFF)
    elif v >= -0x80:
        out.append(0xD0)
        out.append(v & 0xFF)
    elif v >= -0x8000:
        out.append(0xD1)
        out += struct.pack(">h", v)
    elif v >= -0x80000000:
        out.append(0xD2)
        out += struct.pack(">i", v)
    else:
        out.append(0xD3)
        out += struct.pack(">q", v)


def _enc_str(v, out):
    b = v.encode("utf-8")
    n = len(b)
    if n < 32:
        out.append(0xA0 | n)
    elif n < 0x100:
        out.append(0xD9)
        out.append(n)
    elif n < 0x10000:
        out.append(0xDA)
        out += struct.pack(">H", n)
    else:
        out.append(0xDB)
        out += struct.pack(">I", n)
    out += b


def _enc_array(v, out):
    n = len(v)
    if n < 0x10:
        out.append(0x90 | n)
    elif n < 0x10000:
        out.append(0xDC)
        out += struct.pack(">H", n)
    else:
        out.append(0xDD)
        out += struct.pack(">I", n)
    for item in v:
        _enc(item, out)


def _enc_dict(v, out):
    keys = list(v.keys())
    n = len(keys)
    if n < 0x10:
        out.append(0x80 | n)
    elif n < 0x10000:
        out.append(0xDE)
        out += struct.pack(">H", n)
    else:
        out.append(0xDF)
        out += struct.pack(">I", n)
    for k in keys:
        _enc_str(k, out)
        _enc(v[k], out)


class _Cur:
    __slots__ = ("buf", "off")

    def __init__(self, buf, off=0):
        self.buf = buf
        self.off = off


def mp_decode_one(buf, off=0):
    cur = _Cur(buf, off)
    value = _dec(cur)
    return value, cur.off


def _need(cur, n):
    if cur.off + n > len(cur.buf):
        raise Incomplete()


def _be_int(cur, n):
    _need(cur, n)
    v = int.from_bytes(cur.buf[cur.off : cur.off + n], "big")
    cur.off += n
    return v


def _dec(cur):
    _need(cur, 1)
    b = cur.buf[cur.off]
    cur.off += 1
    if b <= 0x7F:
        return b
    if b >= 0xE0:
        return b - 0x100
    if 0xA0 <= b <= 0xBF:
        return _dec_str(cur, b & 0x1F)
    if 0x90 <= b <= 0x9F:
        return _dec_array(cur, b & 0x0F)
    if 0x80 <= b <= 0x8F:
        return _dec_dict(cur, b & 0x0F)
    if b == 0xC0:
        return None
    if b == 0xC2:
        return False
    if b == 0xC3:
        return True
    if b == 0xC4:
        return _dec_bin(cur, _be_int(cur, 1))
    if b == 0xC5:
        return _dec_bin(cur, _be_int(cur, 2))
    if b == 0xC6:
        return _dec_bin(cur, _be_int(cur, 4))
    if b == 0xC7:
        return _dec_ext(cur, _be_int(cur, 1))
    if b == 0xC8:
        return _dec_ext(cur, _be_int(cur, 2))
    if b == 0xC9:
        return _dec_ext(cur, _be_int(cur, 4))
    if b == 0xCA:
        _need(cur, 4)
        v = struct.unpack_from(">f", cur.buf, cur.off)[0]
        cur.off += 4
        return v
    if b == 0xCB:
        _need(cur, 8)
        v = struct.unpack_from(">d", cur.buf, cur.off)[0]
        cur.off += 8
        return v
    if b == 0xCC:
        return _be_int(cur, 1)
    if b == 0xCD:
        return _be_int(cur, 2)
    if b == 0xCE:
        return _be_int(cur, 4)
    if b == 0xCF:
        return _be_int(cur, 8)
    if b == 0xD0:
        v = _be_int(cur, 1)
        return v - 0x100 if v >= 0x80 else v
    if b == 0xD1:
        v = _be_int(cur, 2)
        return v - 0x10000 if v >= 0x8000 else v
    if b == 0xD2:
        v = _be_int(cur, 4)
        return v - 0x100000000 if v >= 0x80000000 else v
    if b == 0xD3:
        v = _be_int(cur, 8)
        return v - 0x10000000000000000 if v >= 0x8000000000000000 else v
    if b == 0xD4:
        return _dec_ext(cur, 1)
    if b == 0xD5:
        return _dec_ext(cur, 2)
    if b == 0xD6:
        return _dec_ext(cur, 4)
    if b == 0xD7:
        return _dec_ext(cur, 8)
    if b == 0xD8:
        return _dec_ext(cur, 16)
    if b == 0xD9:
        return _dec_str(cur, _be_int(cur, 1))
    if b == 0xDA:
        return _dec_str(cur, _be_int(cur, 2))
    if b == 0xDB:
        return _dec_str(cur, _be_int(cur, 4))
    if b == 0xDC:
        return _dec_array(cur, _be_int(cur, 2))
    if b == 0xDD:
        return _dec_array(cur, _be_int(cur, 4))
    if b == 0xDE:
        return _dec_dict(cur, _be_int(cur, 2))
    if b == 0xDF:
        return _dec_dict(cur, _be_int(cur, 4))
    raise ValueError("bad msgpack byte 0x%02x" % b)


def _dec_str(cur, n):
    _need(cur, n)
    v = bytes(cur.buf[cur.off : cur.off + n]).decode("utf-8", "replace")
    cur.off += n
    return v


def _dec_bin(cur, n):
    _need(cur, n)
    v = bytes(cur.buf[cur.off : cur.off + n])
    cur.off += n
    return v


def _dec_ext(cur, n):
    _need(cur, n + 1)
    t = cur.buf[cur.off]
    data = bytes(cur.buf[cur.off + 1 : cur.off + 1 + n])
    cur.off += n + 1
    return {"__nvimcp_ext": t, "data": data.hex()}


def _dec_array(cur, n):
    return [_dec(cur) for _ in range(n)]


def _dec_dict(cur, n):
    d = {}
    for _ in range(n):
        k = _dec(cur)
        d[k if isinstance(k, (str, int)) else str(k)] = _dec(cur)
    return d


class NvimcpProxy:
    def __init__(self, args):
        self.socket_arg = args.socket
        self.pid_arg = args.pid
        self.sessions_file_arg = args.sessions_file
        self.timeout = args.timeout
        self.debug = args.debug
        self.sel = selectors.DefaultSelector()
        self.sock = None
        self.connected = False
        self.nvim_rx = bytearray()
        self.stdin_rx = bytearray()
        self.pending = {}
        self.parked = []
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
                    self._check_timeouts()
        finally:
            self._close_nvim()
            self.sel.close()
        return self.exit_code if self.exit_code is not None else 0

    def _shutdown(self, code):
        self.exit_code = code

    def _select_timeout(self):
        if not self.pending:
            return SELECT_TICK
        now = time.monotonic()
        nearest = min(m["deadline"] for m in self.pending.values())
        return max(0.0, min(nearest - now, SELECT_TICK))

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
            self._shutdown(0)
            return
        if not chunk:
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
                self.parked.append((mcp_id, what, params))
                return
            self._send_nvimcp_rpc(mcp_id, what, params)
        except ProxyError as e:
            self._send_mcp_error(mcp_id, -32603, str(e))

    def _default_sessions_file(self):
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        base = xdg if xdg else "/run/user/%d" % os.getuid()
        return os.path.join(base, "nvimcp", "sessions.json")

    def _describe(self, entries):
        return ", ".join("pid %s (%s)" % (e.get("pid"), e.get("cwd", "?")) for e in entries)

    def _discover(self):
        if self.socket_arg:
            return {"socket": self.socket_arg, "pid": None}
        path = self.sessions_file_arg or self._default_sessions_file()
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            raise ProxyError("no sessions file at %s; is nvim running with nvimcp started?" % path)
        except (OSError, ValueError) as e:
            raise ProxyError("cannot read sessions file %s: %s" % (path, e))
        entries = [e for e in (data or {}).values() if isinstance(e, dict) and isinstance(e.get("socket"), str)]
        if self.pid_arg is not None:
            for e in entries:
                if e.get("pid") == self.pid_arg:
                    return e
            extra = "; available: %s" % self._describe(entries) if entries else ""
            raise ProxyError("no nvimcp session with pid %s in %s%s" % (self.pid_arg, path, extra))
        cwd = os.getcwd()
        cwd_hits = [e for e in entries if e.get("cwd") == cwd]
        if len(cwd_hits) == 1:
            return cwd_hits[0]
        if len(cwd_hits) > 1:
            self._warn("multiple sessions in this directory; picking pid %s" % cwd_hits[0].get("pid"))
            return cwd_hits[0]
        if len(entries) == 1:
            return entries[0]
        if not entries:
            raise ProxyError("no nvimcp sessions in %s; start nvim and run :NvimcpStart" % path)
        raise ProxyError(
            "multiple nvim sessions; pass --pid or --socket. Available: %s" % self._describe(entries)
        )

    def _connect(self):
        if self.sock is not None:
            return
        target = self._discover()
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
    p.add_argument("--pid", type=int, help="pick this nvim pid from the sessions file")
    p.add_argument("--sessions-file", help="path to sessions.json (default: $XDG_RUNTIME_DIR/nvimcp/sessions.json)")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="per-nvim-rpc timeout in seconds (default 30)")
    p.add_argument("--debug", action="store_true", help="log proxy activity to stderr")
    args = p.parse_args()
    sys.exit(NvimcpProxy(args).run())


if __name__ == "__main__":
    main()
