from __future__ import annotations

# JSON-RPC 2.0 standard codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Implementation-defined codes (-32000..-32099 reserved by the spec)
NO_SESSION = -32001
SESSION_TIMEOUT = -32002


class ProxyError(Exception):
    """Failure the proxy can report straight to the MCP client. The class
    carries the JSON-RPC code the app should use when it surfaces str(e)."""

    code: int = INTERNAL_ERROR


class NoSessionError(ProxyError):
    """No reachable nvimcp session: none found, refused, unreachable, or gone."""

    code = NO_SESSION
