#!/usr/bin/env python3
"""Thin launcher: the implementation lives in the nvimcp_proxy package next
to this file. MCP client configs keep pointing at this path.

Names below are re-exported so test loaders that importlib-load this file
keep seeing the same module-level API while the package split lands."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from nvimcp_proxy.errors import ProxyError
from nvimcp_proxy.mp import Incomplete, mp_decode_one, mp_encode
from nvimcp_proxy.bridge import RPC_NOTIFICATION, RPC_REQUEST, RPC_RESPONSE
from nvimcp_proxy.legacy import NvimcpProxy, main

if __name__ == "__main__":
    main()
