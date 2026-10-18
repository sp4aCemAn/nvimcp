import json
import os
import sys

from . import (
    DEFAULT_PROTOCOL_VERSION,
    JSON_DUMP_KW,
    SERVER_NAME,
    SERVER_VERSION,
)


class StdioEndpoint:
    """MCP/JSON-RPC side of the bridge: stdin line framing, method routing,
    stdout responses. Orchestration (parking, connecting, nvim calls) stays
    in the app; the endpoint hands off via callbacks."""

    def __init__(self, on_needs_nvim, on_eof, on_write_error, log):
        self.rx = bytearray()
        self.on_needs_nvim = on_needs_nvim
        self.on_eof = on_eof
        self.on_write_error = on_write_error
        self.log = log

    def poll(self):
        try:
            chunk = os.read(0, 65536)
        except OSError:
            self.on_eof()
            return
        if not chunk:
            self.on_eof()
            return
        self.rx += chunk
        while True:
            i = self.rx.find(b"\n")
            if i < 0:
                break
            line = bytes(self.rx[:i])
            del self.rx[: i + 1]
            self._on_line(line)

    def _on_line(self, line):
        text = line.strip()
        if not text:
            return
        try:
            msg = json.loads(text)
        except ValueError as e:
            self.send_error(None, -32700, "parse error: %s" % e)
            return
        if isinstance(msg, dict) and isinstance(msg.get("method"), str):
            self._on_msg(msg)

    def _on_msg(self, msg):
        mcp_id = msg.get("id")
        if mcp_id is None:
            self.on_notification(msg.get("method"))
            return
        method = msg["method"]
        params = msg.get("params")
        if not isinstance(params, dict):
            params = {}
        if method == "initialize":
            pv = params.get("protocolVersion")
            self.send_result(mcp_id, {
                "protocolVersion": pv if isinstance(pv, str) and pv else DEFAULT_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": "Live Neovim buffers and LSP state via nvimcp tools.",
            })
        elif method == "ping":
            self.send_result(mcp_id, {})
        elif method == "tools/list":
            self.on_needs_nvim(mcp_id, "list_tools", {})
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments")
            if not isinstance(name, str) or not name:
                self.send_error(mcp_id, -32602, "tools/call requires params.name")
            elif arguments is not None and not isinstance(arguments, dict):
                self.send_error(mcp_id, -32602, "params.arguments must be an object")
            else:
                self.on_needs_nvim(mcp_id, "call_tool", {"name": name, "arguments": arguments or {}})
        elif method == "prompts/list":
            self.send_result(mcp_id, {"prompts": []})
        elif method == "resources/list":
            self.send_result(mcp_id, {"resources": []})
        elif method == "resources/templates/list":
            self.send_result(mcp_id, {"resourceTemplates": []})
        elif method == "logging/setLevel":
            self.send_result(mcp_id, {})
        else:
            self.send_error(mcp_id, -32601, "method not found: %s" % method)

    def on_notification(self, method):
        pass

    def result_from_nvimcp(self, meta, result):
        what = meta["what"]
        if not isinstance(result, dict):
            self.send_error(meta["mcp_id"], -32603, "nvimcp returned an unexpected response for %s" % what)
            return
        if result.get("error") is not None:
            if what == "call_tool":
                self.send_result(meta["mcp_id"], {
                    "content": [{"type": "text", "text": str(result["error"])}],
                    "isError": True,
                })
            else:
                self.send_error(meta["mcp_id"], -32603, str(result["error"]))
            return
        if what == "list_tools":
            tools = result.get("tools", [])
            self.send_result(meta["mcp_id"], {"tools": tools if isinstance(tools, list) else []})
        else:
            self.send_result(meta["mcp_id"], {
                "content": [{"type": "text", "text": json.dumps(result.get("result"), **JSON_DUMP_KW)}]
            })

    def send_result(self, mcp_id, result):
        self._send({"jsonrpc": "2.0", "id": mcp_id, "result": result})

    def send_error(self, mcp_id, code, message):
        self._send({"jsonrpc": "2.0", "id": mcp_id, "error": {"code": code, "message": message}})

    def _send(self, msg):
        try:
            sys.stdout.buffer.write((json.dumps(msg, **JSON_DUMP_KW) + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()
        except (BrokenPipeError, OSError):
            self.on_write_error()
