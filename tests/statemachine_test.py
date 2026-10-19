#!/usr/bin/env python3

# Characterization tests for the proxy state machine: parking, connect retry,
# wait windows, timeouts, health handling, framing, and death/reconnect.
# No KNOWN-ISSUE pins remain: bug A (stale wait window) was fixed in
# Phase 1a and bug B (EOF honesty) in Phase 1b; their pins were flipped in
# the same commits as the fixes. When adding a new pin for a found bug,
# mark it KNOWN-ISSUE and flip it only together with the production fix.
#
# Run: python3 tests/statemachine_test.py

import json
import os
import sys
import types
from contextlib import contextmanager

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "proxy"))

from nvimcp_proxy import app as app_mod
from nvimcp_proxy import bridge as bridge_mod
from nvimcp_proxy import mcp as mcp_mod
from nvimcp_proxy.app import NvimcpProxy, CONNECT_RETRY, SELECT_TICK
from nvimcp_proxy.bridge import HEALTH_LUA, NVIMCP_RPC_LUA
from nvimcp_proxy.errors import ProxyError
from nvimcp_proxy.mp import mp_decode_one, mp_encode

n_fail = 0


def ok(name, cond, detail=""):
	global n_fail
	print(("PASS %s" if cond else "FAIL %s") % name + ("" if cond else ": " + str(detail)[:300]))
	if not cond:
		n_fail += 1


class Clock:
	def __init__(self, now=1000.0):
		self.now = now

	def monotonic(self):
		return self.now

	def advance(self, dt):
		self.now += dt


class FakeBuffer:
	def __init__(self, sink):
		self.sink = sink
		self.raise_on_write = False

	def write(self, data):
		if self.raise_on_write:
			raise BrokenPipeError(32, "broken pipe")
		self.sink += data

	def flush(self):
		pass


class FakeStdout:
	REAL = sys.stdout

	def __init__(self, sink):
		self.buffer = FakeBuffer(sink)

	def write(self, data):
		FakeStdout.REAL.write(data)

	def flush(self):
		FakeStdout.REAL.flush()


class FakeSelector:
	def __init__(self):
		self.registered = []

	def register(self, fileobj, events, data):
		self.registered.append((fileobj, data))

	def unregister(self, fileobj):
		self.registered = [r for r in self.registered if r[0] is not fileobj]

	def close(self):
		pass


class FakeSocket:
	def __init__(self, chunks=()):
		self._chunks = list(chunks)
		self.sent = []
		self.path = None
		self.closed = False
		self.connect_error = None
		self.recv_error = None
		self.sendall_error = None

	def settimeout(self, t):
		self.timeout = t

	def connect(self, path):
		if self.connect_error:
			raise self.connect_error
		self.path = path

	def setblocking(self, b):
		pass

	def sendall(self, data):
		if self.sendall_error:
			raise self.sendall_error
		self.sent.append(bytes(data))

	def recv(self, n):
		if self.recv_error:
			raise self.recv_error
		return self._chunks.pop(0) if self._chunks else b""

	def close(self):
		self.closed = True


@contextmanager
def env(wait=10.0, timeout=30.0):
	"""A proxy with a fake clock, fake stdout, and a fake selector.

	The real selector created in __init__ is closed immediately; no real fds
	are used because bridge.connect runs against FakeSocket (see
	patched_socket)."""
	clock = Clock()
	sink = bytearray()
	old = (sys.stdout, app_mod.time, bridge_mod.time)
	stub = types.SimpleNamespace(monotonic=clock.monotonic)
	sys.stdout = FakeStdout(sink)
	app_mod.time = stub
	bridge_mod.time = stub
	args = types.SimpleNamespace(
		socket=None, pid=None, sessions_file=None, skip_ancestry=True,
		timeout=timeout, wait=wait, debug=False,
	)
	p = NvimcpProxy(args)
	real_sel = p.sel
	p.sel = FakeSelector()
	p.bridge.sel = p.sel
	real_sel.close()
	try:
		yield p, clock, sink
	finally:
		sys.stdout, app_mod.time, bridge_mod.time = old


@contextmanager
def patched_socket(s):
	orig = bridge_mod.socket.socket
	bridge_mod.socket.socket = lambda *a, **k: s
	try:
		yield
	finally:
		bridge_mod.socket.socket = orig


def responses(sink):
	out = []
	for line in bytes(sink).decode("utf-8").split("\n"):
		if line.strip():
			out.append(json.loads(line))
	return out


def by_id(sink, mcp_id):
	for r in responses(sink):
		if r.get("id") == mcp_id:
			return r
	return None


def err_text(r):
	return (r or {}).get("error", {}).get("message", "")


def client(p, mcp_id, method, params=None):
	p.mcp._on_line(json.dumps(
		{"jsonrpc": "2.0", "id": mcp_id, "method": method, "params": params or {}}
	).encode("utf-8"))


def feed(p, sock, *msgs):
	sock._chunks.append(b"".join(mp_encode(m) for m in msgs))
	p.bridge.poll()


def establish(p, sock):
	"""Connect + health handshake with no client requests in flight.
	Health takes msgid 1; the first tool request afterwards takes msgid 2."""
	p.finder.discover = lambda: {"socket": "/fake.sock", "pid": 4242}
	with patched_socket(sock):
		p._connect()
	feed(p, sock, [1, 1, None, True])


def no_session(p, msg="no nvimcp sessions found in /fake-default"):
	def boom():
		raise ProxyError(msg)
	p.finder.discover = boom


def t_initialize_local():
	with env() as (p, clock, sink):
		client(p, 1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
		client(p, 2, "ping")
		client(p, 3, "prompts/list")
		client(p, 4, "resources/list")
		client(p, 5, "resources/templates/list")
		client(p, 6, "logging/setLevel", {"level": "info"})
		rs = responses(sink)
		ok("local methods answered without nvim", len(rs) == 6 and not p.parked and not p.bridge.connected, rs)
		init = by_id(sink, 1)
		ok("initialize echoes protocol version", init["result"]["protocolVersion"] == "2025-06-18", init)
		ok("initialize serverInfo", init["result"]["serverInfo"] == {"name": "nvimcp", "version": "0.2.0"}, init)
		ok("initialize capabilities", init["result"]["capabilities"] == {"tools": {"listChanged": False}}, init)
		ok("instructions present", bool(init["result"].get("instructions")), init)


def t_initialize_defaults():
	with env() as (p, clock, sink):
		client(p, 1, "initialize", {})
		ok("missing protocolVersion -> default",
			by_id(sink, 1)["result"]["protocolVersion"] == "2024-11-05", responses(sink))


def t_malformed_input():
	with env() as (p, clock, sink):
		p.mcp._on_line(b"{oops")
		client(p, 1, "tools/call", {})
		client(p, 2, "tools/call", {"name": "x", "arguments": "not-a-dict"})
		client(p, 3, "bogus/method")
		p.mcp._on_line(b'{"jsonrpc": "2.0", "method": "notifications/initialized"}')
		p.mcp._on_line(b'{"jsonrpc": "2.0", "id": 9}')
		rs = responses(sink)
		ok("parse error -32700 with null id",
			by_id(sink, None) and by_id(sink, None)["error"]["code"] == -32700, rs)
		ok("missing name -32602",
			by_id(sink, 1)["error"]["code"] == -32602 and "params.name" in err_text(by_id(sink, 1)), rs)
		ok("bad arguments -32602",
			by_id(sink, 2)["error"]["code"] == -32602 and "must be an object" in err_text(by_id(sink, 2)), rs)
		ok("unknown method -32601",
			by_id(sink, 3)["error"]["code"] == -32601 and "bogus/method" in err_text(by_id(sink, 3)), rs)
		ok("notification and method-less dict ignored", len(rs) == 4 and not p.parked, rs)


def t_park_then_replay_in_order():
	with env() as (p, clock, sink):
		s = FakeSocket()
		p.finder.discover = lambda: {"socket": "/fake.sock", "pid": 4242}
		with patched_socket(s):
			client(p, 1, "tools/list")
			client(p, 2, "tools/call", {"name": "buffer_read", "arguments": {}})
		ok("requests park while health pending", len(p.parked) == 2 and not responses(sink), p.parked)
		hello, _off = mp_decode_one(s.sent[0])
		ok("first frame is the health probe",
			hello == [0, 1, "nvim_exec_lua", [HEALTH_LUA, []]], hello)
		feed(p, s, [1, 1, None, True])
		ok("parked replayed FIFO after health OK",
			p.parked == [] and p.bridge.connected and len(s.sent) == 3, (p.parked, len(s.sent)))
		call, _off = mp_decode_one(s.sent[2])
		ok("replayed call keeps original params",
			call == [0, 3, "nvim_exec_lua", [NVIMCP_RPC_LUA, ["call_tool", {"name": "buffer_read", "arguments": {}}]]], call)
		feed(p, s, [1, 3, None, {"result": {"z": 1}}], [1, 2, None, {"tools": []}])
		ok("client responses follow nvim response order, not request order",
			[r["id"] for r in responses(sink)] == [2, 1], responses(sink))
		ok("call result json-dumped",
			by_id(sink, 2)["result"]["content"][0]["text"] == '{"z":1}', responses(sink))


def t_park_dedups_by_mcp_id():
	with env() as (p, clock, sink):
		p._park(5, "call_tool", {})
		p._park(5, "call_tool", {})
		p._park(6, "list_tools", {})
		ok("park dedups same mcp_id", len(p.parked) == 2, p.parked)


def t_wait_zero_fails_immediately():
	with env(wait=0.0) as (p, clock, sink):
		no_session(p, "nope")
		client(p, 1, "tools/list")
		r = by_id(sink, 1)
		ok("wait=0 -> immediate -32603 with discovery error text",
			r["error"]["code"] == -32603 and err_text(r) == "nope", r)
		ok("nothing parked", not p.parked, p.parked)


def t_wait_window_expiry_errors():
	with env() as (p, clock, sink):
		calls = []
		def boom():
			calls.append(clock.now)
			raise ProxyError("no nvimcp sessions found in /fake-default")
		p.finder.discover = boom
		client(p, 1, "tools/list")
		ok("parked, connect_started stamped, first error remembered",
			len(p.parked) == 1 and p.connect_started == 1000.0
			and p.last_connect_err == "no nvimcp sessions found in /fake-default" and len(calls) == 1,
			(p.parked, p.connect_started, p.last_connect_err, calls))
		p._pump_connect()
		ok("first pump retries discovery immediately", len(calls) == 2 and p.next_connect_retry == 1000.5, (calls, p.next_connect_retry))
		clock.advance(0.4)
		p._pump_connect()
		ok("pump inside CONNECT_RETRY window does not re-discover", len(calls) == 2, calls)
		clock.advance(3.0)
		p._pump_connect()
		ok("pump after CONNECT_RETRY does re-discover", len(calls) == 3, calls)
		ok("still no error before window expiry", not responses(sink), responses(sink))
		clock.advance(6.7)
		p._pump_connect()
		r = by_id(sink, 1)
		ok("window expiry sends waited-Ns error, exact text",
			err_text(r) == "no nvimcp sessions found in /fake-default (waited 10s for an nvim session)", r)
		ok("parked cleared after expiry", not p.parked, p.parked)


def t_wait_window_rearms_after_expiry():
	# Fixed in Phase 1a (was bug A "stale wait window"): a window expiry
	# resets connect_started/retry/backoff/error, so the NEXT parked request
	# gets a real, fresh wait window.
	with env() as (p, clock, sink):
		no_session(p, "gone")
		client(p, 1, "tools/list")
		clock.advance(10.1)
		p._pump_connect()
		ok("first request failed after its own window", by_id(sink, 1) is not None, responses(sink))
		ok("expiry reset the window state",
			p.connect_started is None and p.last_connect_err is None and p.next_connect_retry == 0.0,
			(p.connect_started, p.last_connect_err, p.next_connect_retry))
		client(p, 2, "tools/list")
		p._pump_connect()
		ok("second request parks in a FRESH window (no instant fail)",
			by_id(sink, 2) is None and p.connect_started == 1010.1, (responses(sink), p.connect_started))
		clock.advance(10.1)
		p._pump_connect()
		ok("second request gets its own waited-Ns error",
			"waited 10s" in err_text(by_id(sink, 2)) and not p.parked
			and p.connect_started is None, (by_id(sink, 2), p.connect_started))


def t_window_resets_on_ready():
	# Fixed in Phase 1a: health-ready clears the window so a later failed
	# episode re-arms from scratch instead of inheriting a stale stamp.
	with env() as (p, clock, sink):
		calls = []
		def failing():
			calls.append(1)
			raise ProxyError("gone")
		p.finder.discover = failing
		client(p, 1, "tools/list")
		ok("failed discovery armed the window",
			p.connect_started == 1000.0 and p.last_connect_err == "gone", (p.connect_started, p.last_connect_err))
		s = FakeSocket()
		p.finder.discover = lambda: {"socket": "/fake.sock", "pid": 1}
		clock.advance(CONNECT_RETRY)
		with patched_socket(s):
			p._pump_connect()
		ok("retry connected; window stays armed until health resolves",
			p.bridge.sock is not None and p.connect_started == 1000.0, p.connect_started)
		feed(p, s, [1, 1, None, True])
		ok("health-ready reset the window and replayed parked",
			p.connect_started is None and p.last_connect_err is None
			and p.next_connect_retry == 0.0 and not p.parked,
			(p.connect_started, p.last_connect_err, p.next_connect_retry, p.parked))
		s._chunks.append(b"")
		p.bridge.poll()
		ok("death reset the window too",
			not p.bridge.sock and p.connect_started is None and p.next_connect_retry == 0.0,
			(p.bridge.sock, p.connect_started))
		p.finder.discover = failing
		client(p, 2, "tools/list")
		ok("a post-death park re-arms a fresh window",
			len(calls) == 2 and p.connect_started == clock.now and p.parked == [(2, "list_tools", {})],
			(calls, p.connect_started, p.parked))


def t_health_false_and_err():
	with env() as (p, clock, sink):
		s = FakeSocket()
		p.finder.discover = lambda: {"socket": "/fake.sock", "pid": 4242}
		with patched_socket(s):
			client(p, 1, "tools/list")
		feed(p, s, [1, 1, None, False])
		r = by_id(sink, 1)
		ok("health False -> parked fail with :NvimcpStart reason, socket closed",
			"NvimcpStart" in err_text(r) and not p.bridge.sock and not p.bridge.connected, r)
	with env() as (p, clock, sink):
		s = FakeSocket()
		p.finder.discover = lambda: {"socket": "/fake.sock", "pid": 4242}
		with patched_socket(s):
			client(p, 1, "tools/list")
		feed(p, s, [1, 1, {"message": "module 'nvimcp' not found"}, None])
		r = by_id(sink, 1)
		ok("health error + plugin-not-found hint",
			"nvim rejected the nvimcp health check" in err_text(r)
			and "does not have the nvimcp plugin loaded" in err_text(r), r)


def t_call_tool_error_is_error_result():
	with env() as (p, clock, sink):
		s = FakeSocket()
		establish(p, s)
		client(p, 1, "tools/call", {"name": "x"})
		feed(p, s, [1, 2, None, {"error": "boom"}])
		r = by_id(sink, 1)
		ok("call_tool nvimcp error becomes isError content (not protocol error)",
			r["result"]["isError"] is True and r["result"]["content"][0]["text"] == "boom", r)


def t_non_tool_error_is_protocol_error():
	with env() as (p, clock, sink):
		s = FakeSocket()
		establish(p, s)
		client(p, 1, "tools/list")
		feed(p, s, [1, 2, None, {"error": "boom"}])
		r = by_id(sink, 1)
		ok("list_tools nvimcp error becomes -32603 with verbatim text",
			r["error"]["code"] == -32603 and err_text(r) == "boom", r)


def t_tools_passthrough():
	with env() as (p, clock, sink):
		s = FakeSocket()
		establish(p, s)
		client(p, 1, "tools/list")
		feed(p, s, [1, 2, None, {"tools": [{"name": "t"}]}])
		ok("list_tools passes tools through",
			by_id(sink, 1)["result"]["tools"] == [{"name": "t"}], by_id(sink, 1))
	with env() as (p, clock, sink):
		s = FakeSocket()
		establish(p, s)
		client(p, 1, "tools/list")
		feed(p, s, [1, 2, None, {"tools": "wrong"}])
		ok("list_tools non-list tools -> []",
			by_id(sink, 1)["result"]["tools"] == [], by_id(sink, 1))


def t_non_dict_result():
	with env() as (p, clock, sink):
		s = FakeSocket()
		establish(p, s)
		client(p, 1, "tools/call", {"name": "x"})
		feed(p, s, [1, 2, None, "surprise"])
		ok("non-dict result -> unexpected response error",
			"unexpected response for call_tool" in err_text(by_id(sink, 1)), by_id(sink, 1))


def t_rpc_level_error():
	with env() as (p, clock, sink):
		s = FakeSocket()
		establish(p, s)
		client(p, 1, "tools/call", {"name": "x"})
		feed(p, s, [1, 2, {"message": "kaboom"}, None])
		ok("rpc-level err (non-health) -> -32603 nvim rpc failed",
			by_id(sink, 1)["error"]["code"] == -32603 and err_text(by_id(sink, 1)) == "nvim rpc failed: kaboom",
			by_id(sink, 1))
		ok("rpc-level error does NOT kill the connection", p.bridge.connected and p.bridge.sock is s, p.bridge.connected)


def t_call_result_encoding():
	with env() as (p, clock, sink):
		s = FakeSocket()
		establish(p, s)
		client(p, 1, "tools/call", {"name": "x"})
		feed(p, s, [1, 2, None, {"result": {"a": [1, 2], "b": "é"}}])
		ok("call_tool result json-dumped compact, non-ascii preserved",
			by_id(sink, 1)["result"]["content"][0]["text"] == '{"a":[1,2],"b":"é"}', by_id(sink, 1))


def t_ignores_odd_frames():
	with env() as (p, clock, sink):
		s = FakeSocket()
		establish(p, s)
		feed(p, s, [2], [0, 5, "m", [1]], [1, 999, None, {"result": 1}])
		ok("short/non-response/unknown-msgid frames ignored without output",
			not responses(sink), responses(sink))


def t_mcp_request_timeout():
	with env() as (p, clock, sink):
		s = FakeSocket()
		establish(p, s)
		client(p, 1, "tools/call", {"name": "x"})
		clock.advance(31.0)
		p.bridge.check_timeouts(clock.monotonic())
		r = by_id(sink, 1)
		ok("mcp request timeout text exact",
			err_text(r) == "nvim rpc timed out after 30s (call_tool)", r)
		ok("timed-out request dropped from pending, connection kept",
			not p.bridge.pending and p.bridge.connected, (p.bridge.pending, p.bridge.connected))


def t_health_timeout_kills_session():
	with env() as (p, clock, sink):
		s = FakeSocket()
		p.finder.discover = lambda: {"socket": "/fake.sock", "pid": 4242}
		with patched_socket(s):
			client(p, 1, "tools/list")
		clock.advance(31.0)
		p.bridge.check_timeouts(clock.monotonic())
		r = by_id(sink, 1)
		ok("health timeout kills parked with exact reason",
			err_text(r) == "nvim health check timed out" and not p.bridge.sock and not p.parked, r)


def t_partial_frames():
	with env() as (p, clock, sink):
		s = FakeSocket()
		establish(p, s)
		client(p, 1, "tools/call", {"name": "x"})
		frame = mp_encode([1, 2, None, {"result": 1}])
		s._chunks.append(frame[:3])
		p.bridge.poll()
		ok("partial frame buffers without response",
			not by_id(sink, 1) and len(p.bridge.rx) == 3, (responses(sink), bytes(p.bridge.rx)))
		s._chunks.append(frame[3:])
		p.bridge.poll()
		ok("completing frame decodes and replies",
			by_id(sink, 1)["result"]["content"][0]["text"] == "1", responses(sink))


def t_concatenated_frames():
	with env() as (p, clock, sink):
		s = FakeSocket()
		establish(p, s)
		client(p, 1, "tools/call", {"name": "a"})
		client(p, 2, "tools/call", {"name": "b"})
		feed(p, s, [1, 2, None, {"result": 1}], [1, 3, None, {"result": 2}])
		ok("concatenated frames both processed in one poll",
			by_id(sink, 1)["result"]["content"][0]["text"] == "1"
			and by_id(sink, 2)["result"]["content"][0]["text"] == "2", responses(sink))


def t_socket_death_paths():
	cases = [
		("recv empty", [], None, "nvim session disconnected"),
		("recv OSError", [], OSError("ebadf"), "nvim socket error: ebadf"),
		("decode error", [b"\xc1"], None, "msgpack decode error: bad msgpack byte 0xc1"),
	]
	for label, chunks, recv_err, want in cases:
		with env() as (p, clock, sink):
			s = FakeSocket()
			establish(p, s)
			client(p, 1, "tools/call", {"name": "x"})
			s._chunks.extend(chunks)
			s.recv_error = recv_err
			p.bridge.poll()
			ok("death path %s: exact reason, socket closed" % label,
				err_text(by_id(sink, 1)) == want and not p.bridge.sock, (by_id(sink, 1), want))


def t_sendall_failure():
	with env() as (p, clock, sink):
		s = FakeSocket()
		establish(p, s)
		s.sendall_error = OSError("write boom")
		client(p, 1, "tools/call", {"name": "x"})
		ok("sendall OSError -> die with write-failed reason, pending cleared",
			err_text(by_id(sink, 1)) == "nvim socket write failed: write boom"
			and not p.bridge.sock and not p.bridge.pending, (by_id(sink, 1), p.bridge.pending))


def t_died_fails_parked_and_pending():
	with env() as (p, clock, sink):
		s = FakeSocket()
		establish(p, s)
		client(p, 1, "tools/call", {"name": "x"})
		p._park(2, "list_tools", {})
		s._chunks.append(b"")
		p.bridge.poll()
		ok("die fails pending AND parked with the same reason",
			err_text(by_id(sink, 1)) == "nvim session disconnected"
			and err_text(by_id(sink, 2)) == "nvim session disconnected"
			and not p.parked, responses(sink))


def t_reconnect_after_death():
	with env() as (p, clock, sink):
		s = FakeSocket()
		establish(p, s)
		s2 = FakeSocket()
		calls = []
		def discover():
			calls.append(len(calls))
			return {"socket": "/fake2.sock", "pid": 99}
		p.finder.discover = discover
		s._chunks.append(b"")
		p.bridge.poll()
		ok("connection died cleanly first", not p.bridge.sock and not p.bridge.connected, p.bridge.sock)
		with patched_socket(s2):
			client(p, 1, "tools/list")
		ok("after death, next request rediscovers and health-pings again",
			len(calls) == 1 and s2.path == "/fake2.sock" and len(s2.sent) == 1, (calls, s2.path, len(s2.sent)))
		health_msgid = p.bridge.next_msgid - 1
		feed(p, s2, [1, health_msgid, None, True])
		ok("reconnect health OK flips connected back", p.bridge.connected, p.bridge.connected)
		ok("parked request went out on the fresh connection", len(s2.sent) == 2, len(s2.sent))


def t_connect_error_translation():
	s = FakeSocket()
	s.connect_error = PermissionError(13, "denied")
	with env() as (p, clock, sink):
		p.finder.discover = lambda: {"socket": "/nope.sock", "pid": 1}
		with patched_socket(s):
			client(p, 1, "tools/list")
		ok("socket connect OSError parked with translated ProxyError text, no crash",
			len(p.parked) == 1 and "/nope.sock" in p.last_connect_err
			and "is the nvim session still alive?" in p.last_connect_err, p.last_connect_err)


def t_eof_reports_parked_without_last_ditch_connect():
	# Fixed in Phase 1b (was bug B): EOF no longer promises a "one last
	# discovery try". It reports the pending discovery error to every parked
	# request, resets the window, and exits — deterministically, because a
	# fresh async connect could never complete before the loop tears down.
	with env() as (p, clock, sink):
		calls = []
		def boom():
			calls.append(clock.now)
			raise ProxyError("gone")
		p.finder.discover = boom
		client(p, 1, "tools/list")
		p._pump_connect()
		ok("retry slot taken by pump", len(calls) == 2 and p.next_connect_retry > clock.now, (calls, p.next_connect_retry))
		p._on_stdin_eof()
		ok("EOF attempts no further discovery (by design)", len(calls) == 2, calls)
		ok("EOF flush reports raw last_connect_err per parked id and resets window",
			err_text(by_id(sink, 1)) == "gone" and not p.parked
			and p.connect_started is None and p.last_connect_err is None,
			(by_id(sink, 1), p.connect_started, p.last_connect_err))
		ok("EOF exits 0", p.exit_code == 0, p.exit_code)


def t_eof_with_gate_eligible_reports_instead_of_connecting():
	# Fixed in Phase 1b (bug B twin): previously, if the retry gate had
	# elapsed, EOF would start a doomed connect and silently drop parked
	# requests. Now EOF always reports them.
	with env() as (p, clock, sink):
		no_session(p, "gone")
		client(p, 1, "tools/list")
		calls = []
		def succeed():
			calls.append(1)
			return {"socket": "/late.sock", "pid": 7}
		p.finder.discover = succeed
		clock.advance(CONNECT_RETRY + 0.1)
		p._on_stdin_eof()
		ok("EOF reports parked instead of starting a doomed connect",
			err_text(by_id(sink, 1)) == "gone" and p.bridge.sock is None
			and not p.parked and p.exit_code == 0 and not calls,
			(responses(sink), p.bridge.sock, calls))


def t_eof_with_health_pending_reports_parked_generic():
	with env() as (p, clock, sink):
		s = FakeSocket()
		p.finder.discover = lambda: {"socket": "/fake.sock", "pid": 1}
		with patched_socket(s):
			client(p, 1, "tools/list")
		ok("health pending, parked waiting, no error stamp",
			p.bridge.sock is s and p.last_connect_err is None, p.last_connect_err)
		p._on_stdin_eof()
		ok("EOF with pending health still reports parked (generic reason)",
			err_text(by_id(sink, 1)) == "no nvimcp session found" and p.exit_code == 0, by_id(sink, 1))


def t_stdout_broken_pipe_exits_1():
	with env() as (p, clock, sink):
		client(p, 1, "ping")
		sys.stdout.buffer.raise_on_write = True
		client(p, 2, "ping")
		ok("stdout write failure exits with code 1", p.exit_code == 1, p.exit_code)


def t_select_timeout_math():
	with env() as (p, clock, sink):
		ok("idle tick is SELECT_TICK", p._select_timeout() == SELECT_TICK, p._select_timeout())
		p.bridge.pending[99] = {"kind": "mcp", "mcp_id": 9, "what": "x", "deadline": clock.now + 0.2}
		ok("nearest pending deadline caps the tick", abs(p._select_timeout() - 0.2) < 1e-9, p._select_timeout())
		del p.bridge.pending[99]
		p.parked = [(1, "list_tools", {})]
		p.connect_started = clock.now
		clock.advance(9.8)
		ok("remaining wait window caps the tick", abs(p._select_timeout() - 0.2) < 1e-9, p._select_timeout())


def t_stdin_line_framing_and_eof():
	with env() as (p, clock, sink):
		chunks = iter([b'{"jsonrpc": "2.0", "id"', b': 1, "method": "ping"}\n', b""])
		orig_read = mcp_mod.os.read
		mcp_mod.os.read = lambda fd, n: next(chunks)
		try:
			p.mcp.poll()
			ok("partial stdin line held until newline", not responses(sink), responses(sink))
			p.mcp.poll()
			ok("completed line answered", by_id(sink, 1)["result"] == {}, responses(sink))
			p.mcp.poll()
			ok("EOF chunk triggers parked flush + shutdown(0)", p.exit_code == 0, p.exit_code)
		finally:
			mcp_mod.os.read = orig_read


def main():
	for t in [
		t_initialize_local,
		t_initialize_defaults,
		t_malformed_input,
		t_park_then_replay_in_order,
		t_park_dedups_by_mcp_id,
		t_wait_zero_fails_immediately,
		t_wait_window_expiry_errors,
		t_wait_window_rearms_after_expiry,
		t_window_resets_on_ready,
		t_health_false_and_err,
		t_call_tool_error_is_error_result,
		t_non_tool_error_is_protocol_error,
		t_tools_passthrough,
		t_non_dict_result,
		t_rpc_level_error,
		t_call_result_encoding,
		t_ignores_odd_frames,
		t_mcp_request_timeout,
		t_health_timeout_kills_session,
		t_partial_frames,
		t_concatenated_frames,
		t_socket_death_paths,
		t_sendall_failure,
		t_died_fails_parked_and_pending,
		t_reconnect_after_death,
		t_connect_error_translation,
		t_eof_reports_parked_without_last_ditch_connect,
		t_eof_with_gate_eligible_reports_instead_of_connecting,
		t_eof_with_health_pending_reports_parked_generic,
		t_stdout_broken_pipe_exits_1,
		t_select_timeout_math,
		t_stdin_line_framing_and_eof,
	]:
		t()
	if n_fail > 0:
		print("statemachine tests: %d failures" % n_fail)
		sys.exit(1)
	print("statemachine tests: all passed")


try:
	main()
except Exception:
	import traceback
	traceback.print_exc()
	print("statemachine tests: aborted")
	sys.exit(1)
