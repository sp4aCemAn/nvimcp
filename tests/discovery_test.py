#!/usr/bin/env python3

# Unit tests for proxy session discovery helpers (_session_alive, _related,
# _rank, _sessions_files) — platform independent, no nvim required.

import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "proxy"))

from nvimcp_proxy import discovery as discovery_mod
from nvimcp_proxy.discovery import SessionFinder

n_fail = 0


def ok(name, cond, detail=""):
	global n_fail
	print(("PASS %s" if cond else "FAIL %s") % name + ("" if cond else ": " + str(detail)[:200]))
	if not cond:
		n_fail += 1


def make_finder():
	return SessionFinder(None, None, None, False, lambda m: None, lambda m: None)


def main():
	p = make_finder()

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

		chain = p._ancestor_pid_chain()
		ok("ancestor chain starts at own parent", len(chain) > 0 and chain[0] == os.getppid(),
			"chain[0]=%r ppid=%r" % (chain[0] if chain else None, os.getppid()))
		ok("ancestor chain is bounded ints", all(isinstance(x, int) and x > 1 for x in chain), chain)
		ok("ancestor chain has no duplicates... bounded growth", len(chain) <= 32, chain)

		# ancestry is cached: discovery retries must not re-walk ps(1) per pass
		calls = [0]
		def counting_walk():
			calls[0] += 1
			return chain
		p2 = make_finder()
		p2._ancestor_pid_chain = counting_walk
		a1 = p2._ancestor_pids()
		a2 = p2._ancestor_pids()
		a3 = p2._ancestor_pids()
		ok("ancestor pids cached across discovery retries",
			calls[0] == 1 and a1 is a2 is a3 and a1 == chain, "walks=%d" % calls[0])

		# Phase 4: on Linux the chain must come from /proc with zero
		# subprocess spawns; same invariants as the ps path.
		if sys.platform.startswith("linux"):
			ok("proc ppid of self equals getppid",
				p._ppid_via_proc(os.getpid()) == os.getppid(), p._ppid_via_proc(os.getpid()))
			ok("proc ppid of a never-existing pid is None",
				p._ppid_via_proc(2 ** 30) is None, p._ppid_via_proc(2 ** 30))
			spawns = []
			orig_run = discovery_mod.subprocess.run
			def counting(*a, **k):
				spawns.append(1)
				return orig_run(*a, **k)
			discovery_mod.subprocess.run = counting
			try:
				proc_chain = make_finder()._ancestor_pid_chain()
			finally:
				discovery_mod.subprocess.run = orig_run
			ok("linux chain walks /proc without spawning ps(1)",
				not spawns and proc_chain and proc_chain[0] == os.getppid()
				and all(x > 1 for x in proc_chain), spawns)

	if n_fail > 0:
		print("discovery tests: %d failures" % n_fail)
		sys.exit(1)
	print("discovery tests: all passed")


try:
	main()
except Exception as e:
	print("discovery tests: aborted: %s" % e)
	sys.exit(1)
