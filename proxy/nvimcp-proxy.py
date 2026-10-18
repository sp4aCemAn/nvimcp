#!/usr/bin/env python3
"""Thin launcher: the implementation lives in the nvimcp_proxy package next
to this file. MCP client configs keep pointing at this path.

Names below are re-exported so test loaders that importlib-load this file
keep seeing the same module-level API while the package split lands."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from nvimcp_proxy.legacy import (
    NvimcpProxy,
    ProxyError,
    Incomplete,
    RPC_REQUEST,
    RPC_RESPONSE,
    RPC_NOTIFICATION,
    mp_encode,
    mp_decode_one,
    main,
)

if __name__ == "__main__":
    main()
