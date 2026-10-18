#!/usr/bin/env python3
"""Thin launcher: the implementation lives in the nvimcp_proxy package next
to this file. MCP client configs keep pointing at this path."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from nvimcp_proxy.app import main

if __name__ == "__main__":
    main()
