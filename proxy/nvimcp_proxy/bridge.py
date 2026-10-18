from __future__ import annotations

import json
import selectors
import socket
import time
from typing import Any, Callable, Dict, List, Optional

from . import JSON_DUMP_KW
from .discovery import SessionEntry
from .errors import ProxyError
from .mp import Incomplete, mp_decode_one, mp_encode

RPC_REQUEST = 0
RPC_RESPONSE = 1
RPC_NOTIFICATION = 2

NVIMCP_RPC_LUA = "return require('nvimcp.server').rpc(...)"
HEALTH_LUA = "return require('nvimcp.server').socket_path() ~= nil"

RequestMeta = Dict[str, Any]


class NvimBridge:
    """nvim msgpack-RPC side: the unix socket, the pending request table and
    the health probe. send_error/result_from write to the MCP endpoint;
    on_died/on_ready notify the app about the connection lifecycle."""

    def __init__(
        self,
        timeout: float,
        log: Callable[[str], None],
        sel: selectors.BaseSelector,
        send_error: Callable[[Any, int, str], None],
        result_from: Callable[[RequestMeta, Any], None],
        on_died: Callable[[str], None],
        on_ready: Callable[[], None],
    ) -> None:
        self.timeout = timeout
        self.log = log
        self.sel = sel
        self.send_error = send_error
        self.result_from = result_from
        self.on_died = on_died
        self.on_ready = on_ready
        self.sock: Optional[socket.socket] = None
        self.connected: bool = False
        self.rx = bytearray()
        self.pending: Dict[int, RequestMeta] = {}
        self.next_msgid: int = 1

    def connect(self, target: SessionEntry) -> None:
        if self.sock is not None:
            return
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
        self.rx = bytearray()
        self.sel.register(s, selectors.EVENT_READ, "nvim")
        self._request("nvim_exec_lua", [HEALTH_LUA, []], {"kind": "health", "pid": target.get("pid")})

    def send_tool(self, mcp_id: Any, what: str, params: Dict[str, Any]) -> None:
        self._request(
            "nvim_exec_lua",
            [NVIMCP_RPC_LUA, [what, params]],
            {"kind": "mcp", "mcp_id": mcp_id, "what": what},
        )

    def _request(self, method: str, params: List[Any], meta: RequestMeta) -> None:
        msgid = self.next_msgid
        self.next_msgid += 1
        meta["deadline"] = time.monotonic() + self.timeout
        self.pending[msgid] = meta
        try:
            self.sock.sendall(mp_encode([RPC_REQUEST, msgid, method, params]))
        except OSError as e:
            self.die("nvim socket write failed: %s" % e)

    def poll(self) -> None:
        try:
            chunk = self.sock.recv(65536)
        except BlockingIOError:
            return
        except OSError as e:
            self.die("nvim socket error: %s" % e)
            return
        if not chunk:
            self.die("nvim session disconnected")
            return
        self.rx += chunk
        while True:
            try:
                value, off = mp_decode_one(self.rx, 0)
            except Incomplete:
                return
            except ValueError as e:
                self.die("msgpack decode error: %s" % e)
                return
            del self.rx[:off]
            self._handle(value)

    def _handle(self, value: Any) -> None:
        if not isinstance(value, list) or len(value) < 4 or value[0] != RPC_RESPONSE:
            return
        msgid, err, result = value[1], value[2], value[3]
        meta = self.pending.pop(msgid, None)
        if meta is None:
            return
        if err is not None:
            text = self._err_text(err)
            if meta["kind"] == "health":
                self.die("nvim rejected the nvimcp health check: %s%s" % (text, self._plugin_hint(text)))
            else:
                self.send_error(meta["mcp_id"], -32603, "nvim rpc failed: %s" % text)
            return
        if meta["kind"] == "health":
            if result is True:
                self.connected = True
                self.log("connected to nvim pid %s" % meta.get("pid"))
                self.on_ready()
            else:
                self.die("nvim has nvimcp loaded but the server is not started (run :NvimcpStart in nvim)")
            return
        self.result_from(meta, result)

    def _err_text(self, err: Any) -> str:
        if isinstance(err, dict):
            msg = err.get("message")
            return str(msg) if msg else json.dumps(err, **JSON_DUMP_KW)
        return str(err)

    def _plugin_hint(self, text: str) -> str:
        if "nvimcp" in text and "not found" in text:
            return "; the nvim session does not have the nvimcp plugin loaded"
        return ""

    def check_timeouts(self, now: float) -> None:
        for msgid in list(self.pending):
            meta = self.pending[msgid]
            if meta["deadline"] > now:
                continue
            if meta["kind"] == "health":
                self.die("nvim health check timed out")
                return
            del self.pending[msgid]
            self.send_error(
                meta["mcp_id"], -32603, "nvim rpc timed out after %.0fs (%s)" % (self.timeout, meta["what"])
            )

    def die(self, reason: str) -> None:
        self.close()
        for msgid in list(self.pending):
            meta = self.pending.pop(msgid)
            if meta["kind"] == "mcp":
                self.send_error(meta["mcp_id"], -32603, reason)
        self.on_died(reason)

    def close(self) -> None:
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
        self.rx = bytearray()
