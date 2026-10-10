#!/usr/bin/env python3

import importlib.util
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY_PATH = os.path.join(HERE, "..", "proxy", "nvimcp-proxy.py")

spec = importlib.util.spec_from_file_location("nvimcp_proxy", PROXY_PATH)
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)

n_fail = 0


def ok(name, cond, detail=""):
	global n_fail
	print(("PASS %s" if cond else "FAIL %s") % name + ("" if cond else ": " + str(detail)[:200]))
	if not cond:
		n_fail += 1


def roundtrip(name, v, expected_bytes=None):
	buf = proxy.mp_encode(v)
	if expected_bytes is not None:
		ok(name + " bytes", buf == expected_bytes, buf.hex())
	value, off = proxy.mp_decode_one(buf)
	ok(name + " roundtrip", value == v and off == len(buf), repr(value))
	return buf


ok("bool is not int", proxy.mp_encode(True) == b"\xc3" and proxy.mp_encode(False) == b"\xc2")
roundtrip("nil", None, b"\xc0")
roundtrip("true", True, b"\xc3")
roundtrip("false", False, b"\xc2")
roundtrip("pos fixint", 5, b"\x05")
roundtrip("pos uint8", 200, b"\xcc\xc8")
roundtrip("pos uint16", 256, b"\xcd\x01\x00")
roundtrip("pos uint32", 0xFFFFFFFF, b"\xce\xff\xff\xff\xff")
roundtrip("pos uint64", 2**53 + 1)
roundtrip("neg fixint", -1, b"\xff")
roundtrip("neg int8", -100, b"\xd0\x9c")
roundtrip("neg int16", -300, b"\xd1\xfe\xd4")
roundtrip("neg int32", -2**31)
roundtrip("neg int64", -2**53 - 1)
roundtrip("float64", 0.5, b"\xcb" + struct.pack(">d", 0.5))
roundtrip("empty str", "", b"\xa0")
roundtrip("fixstr", "AB", b"\xa2AB")
roundtrip("str8", "x" * 255)
roundtrip("str16", "x" * 256)
roundtrip("utf8 str", "héllo 世界 🎉")
roundtrip("empty array", [], b"\x90")
roundtrip("fixarray", [1], b"\x91\x01")
roundtrip("array16", list(range(20)))
roundtrip("empty map", {}, b"\x80")
roundtrip("fixmap", {"a": 1}, b"\x81\xa1a\x01")
roundtrip("map16", {("k%03d" % i): i for i in range(20)})
roundtrip("nested", [1, "two", None, [3, {"four": False, "five": [0.25, -7]}]])

value, _ = proxy.mp_decode_one(b"\xd0\xfb")
ok("decode int8 -5", value == -5, value)
value, _ = proxy.mp_decode_one(b"\xcd\x01\x00")
ok("decode uint16 256", value == 256, value)
value, _ = proxy.mp_decode_one(b"\xcb" + struct.pack(">d", 2.5))
ok("decode float64 2.5", value == 2.5, value)
value, _ = proxy.mp_decode_one(b"\xa2AB")
ok("decode fixstr", value == "AB", value)

rpc_msg = [
	proxy.RPC_REQUEST,
	42,
	"nvim_exec_lua",
	[
		"return require('nvimcp.server').rpc(...)",
		["call_tool", {"name": "buffer_read", "arguments": {"start_line": 1, "end_line": 4}}],
	],
]
buf = proxy.mp_encode(rpc_msg)
value, off = proxy.mp_decode_one(buf)
ok("rpc message roundtrip", value == rpc_msg and off == len(buf), repr(value))

try:
	proxy.mp_decode_one(buf[:-1])
	ok("truncated raises Incomplete", False, "no exception")
except proxy.Incomplete:
	ok("truncated raises Incomplete", True)
except Exception as e:
	ok("truncated raises Incomplete", False, repr(e))

two = proxy.mp_encode([1, 2]) + proxy.mp_encode("x") + proxy.mp_encode({"y": 3})
v1, off1 = proxy.mp_decode_one(two)
v2, off2 = proxy.mp_decode_one(two, off1)
v3, off3 = proxy.mp_decode_one(two, off2)
ok("concatenated messages decode in sequence", v1 == [1, 2] and v2 == "x" and v3 == {"y": 3} and off3 == len(two))

if n_fail > 0:
	print("msgpack tests: %d failures" % n_fail)
	sys.exit(1)
print("msgpack tests: all passed")
