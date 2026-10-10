#!/usr/bin/env python3

import json
import os
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.path.join(HERE, "..", "proxy", "nvimcp-proxy.py")

n_fail = 0


def ok(name, cond, detail=""):
	global n_fail
	print(("PASS %s" if cond else "FAIL %s") % name + ("" if cond else ": " + str(detail)[:300]))
	if not cond:
		n_fail += 1


def main():
	proc = subprocess.Popen(
		[sys.executable, PROXY] + sys.argv[1:],
		stdin=subprocess.PIPE,
		stdout=subprocess.PIPE,
	)
	watchdog = threading.Timer(60, proc.kill)
	watchdog.start()
	next_id = 0

	def req(method, params=None):
		nonlocal next_id
		next_id += 1
		msg = {"jsonrpc": "2.0", "id": next_id, "method": method, "params": params or {}}
		proc.stdin.write((json.dumps(msg) + "\n").encode())
		proc.stdin.flush()
		line = proc.stdout.readline()
		if not line:
			raise RuntimeError("proxy closed stdout while waiting for %s" % method)
		return json.loads(line)

	init = req("initialize", {
		"protocolVersion": "2024-11-05",
		"capabilities": {},
		"clientInfo": {"name": "nvimcp-test", "version": "0"},
	})
	ok("initialize", init.get("result", {}).get("serverInfo", {}).get("name") == "nvimcp"
		and isinstance(init.get("result", {}).get("capabilities", {}).get("tools"), dict), init)
	ok("initialize echoes protocol version", init.get("result", {}).get("protocolVersion") == "2024-11-05", init)

	proc.stdin.write((json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n").encode())
	proc.stdin.flush()

	ping = req("ping")
	ok("ping", ping.get("result") == {} and "error" not in ping, ping)

	tools = req("tools/list")
	names = [t.get("name") for t in tools.get("result", {}).get("tools", [])]
	for expected in ("buffer_read", "buffer_edit", "buffer_search", "lsp_diagnostics"):
		ok("tools/list has %s" % expected, expected in names, names)
	schemas = [t.get("inputSchema", {}).get("type") for t in tools.get("result", {}).get("tools", [])]
	ok("tools/list schemas are objects", schemas and all(s == "object" for s in schemas), schemas)

	read1 = req("tools/call", {"name": "buffer_read", "arguments": {}})
	text1 = read1.get("result", {}).get("content", [{}])[0].get("text", "")
	ok("tools/call buffer_read", "original line one" in text1 and not read1.get("result", {}).get("isError"), text1)

	edit = req("tools/call", {"name": "buffer_edit", "arguments": {"start_line": 2, "end_line": 2, "text": "edited via mcp", "force": True}})
	ok("tools/call buffer_edit", "result" in edit and not edit.get("result", {}).get("isError"), edit)

	read2 = req("tools/call", {"name": "buffer_read", "arguments": {}})
	text2 = read2.get("result", {}).get("content", [{}])[0].get("text", "")
	ok("edit visible over mcp", "edited via mcp" in text2, text2)

	bad = req("tools/call", {"name": "no_such_tool", "arguments": {}})
	ok("unknown tool isError", bad.get("result", {}).get("isError") is True
		and "unknown tool" in bad.get("result", {}).get("content", [{}])[0].get("text", ""), bad)

	bogus = req("bogus/method")
	ok("unknown method -32601", bogus.get("error", {}).get("code") == -32601, bogus)

	proc.stdin.close()
	code = proc.wait(timeout=10)
	ok("proxy exits cleanly on stdin EOF", code == 0, code)

	watchdog.cancel()
	if n_fail > 0:
		print("proxy tests: %d failures" % n_fail)
		sys.exit(1)
	print("proxy tests: all passed")


try:
	main()
except Exception as e:
	print("proxy tests: aborted: %s" % e)
	sys.exit(1)
