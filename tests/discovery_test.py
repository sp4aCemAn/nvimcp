#!/usr/bin/env python3

# Unit tests for proxy session discovery helpers (_session_alive, _related,
# _rank, _sessions_files) — platform independent, no nvim required.

import importlib.util
import os
import subprocess
import sys
import tempfile
import types

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


def make_proxy():
	args = types.SimpleNamespace(socket=None, pid=None, sessions_file=None, timeout=30.0, debug=False)
	return proxy.NvimcpProxy(args)


def main():
	p = make_proxy()

	ok("alive: own pid", p._session_alive({"pid": os.getpid()}) is True)
	dead = subprocess.Popen(["true"])
	dead_pid = dead.pid
	dead.wait()
	ok("alive: reaped pid", p._session_alive({"pid": dead_pid}) is False)
	ok("alive: non-int pid trusted", p._session_alive({"pid": "123"}) is True)
	ok("alive: missing pid trusted", p._session_alive({}) is True)
	ok("alive: pid 0 never signalled", p._session_alive({"pid": 0}) is True)
	ok("alive: negative pid trusted", p._session_alive({"pid": -1}) is True)

	with tempfile.TemporaryDirectory() as root:
		sub = os.path.join(root, "sub")
		deep = os.path.join(sub, "deep")
		os.mkdir(sub)
		os.mkdir(deep)
		other = os.path.join(root, "other")
		os.mkdir(other)

		ok("related: exact", p._related(root, root) is True)
		ok("related: session is ancestor", p._related(root, sub) is True)
		ok("related: session is descendant", p._related(sub, root) is True)
		ok("related: sibling dirs", p._related(sub, other) is False)
		ok("related: no session cwd", p._related(None, sub) is False)
		ok("related: no client cwd", p._related(root, None) is False)
		ok("related: empty session cwd", p._related("", sub) is False)

		cli = sub
		deeper = os.path.join(deep, "deeper")
		os.mkdir(deeper)
		ok("rank: exact cwd", p._rank({"cwd": sub}, cli) == 0)
		ok("rank: one ancestor up", p._rank({"cwd": root}, cli) == 1)
		ok("rank: one level down", p._rank({"cwd": deep}, cli) == 1)
		ok("rank: two levels down", p._rank({"cwd": deeper}, cli) == 2)
		ok("rank: ancestor ties with one-level descendant", p._rank({"cwd": root}, cli) == p._rank({"cwd": deep}, cli))
		ok("rank: ancestor beats two-level descendant", p._rank({"cwd": root}, cli) < p._rank({"cwd": deeper}, cli))
		ok("rank: unrelated", p._rank({"cwd": other}, cli) == 99)
		ok("rank: missing cwd", p._rank({}, cli) == 99)
		ok("rank: empty cwd is not client cwd", p._rank({"cwd": ""}, cli) == 99)

		files = p._sessions_files()
		ok("sessions_files always returns a list", isinstance(files, list), files)

	if n_fail > 0:
		print("discovery tests: %d failures" % n_fail)
		sys.exit(1)
	print("discovery tests: all passed")


try:
	main()
except Exception as e:
	print("discovery tests: aborted: %s" % e)
	sys.exit(1)
