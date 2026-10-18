"""nvimcp stdio proxy implementation package (Python stdlib only)."""

from __future__ import annotations

from typing import Any, Dict

SERVER_NAME: str = "nvimcp"
SERVER_VERSION: str = "0.2.0"
DEFAULT_PROTOCOL_VERSION: str = "2024-11-05"

JSON_DUMP_KW: Dict[str, Any] = {"ensure_ascii": False, "separators": (",", ":")}
