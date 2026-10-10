#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail=0

if ! command -v python3 >/dev/null 2>&1; then
	echo "SKIP proxy tests: python3 not found"
	exit 0
fi

python3 tests/msgpack_test.py || fail=1
python3 tests/discovery_test.py || fail=1

NVIM=$(command -v nvim)
NVIMTERMINALS_PATH="${NVIMTERMINALS_PATH:-$HOME/vsc/nvimterminals}" "$NVIM" --headless -l tests/boot.lua &
bootpid=$!
trap 'touch tests/.stop; wait "$bootpid" 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
	[ -s tests/.boot.sock ] && break
	sleep 0.2
done
SOCK=$(cat tests/.boot.sock)
BOOTPID=$(cat tests/.boot.pid)
echo "target socket: $SOCK (pid $BOOTPID)"

python3 tests/proxy_test.py --socket "$SOCK" || fail=1
python3 tests/proxy_test.py --pid "$BOOTPID" || fail=1
# The harness shell itself may be a descendant of the user's own live nvim
# session (e.g. opencode running inside a term); --skip-ancestry keeps these
# cwd-based e2e cases pinned to the boot fixture.
(cd tests && python3 ../tests/proxy_test.py --skip-ancestry) || fail=1        # exact cwd match -> boot session (tests/ cwd)
mkdir -p tests/e2e-sub
(cd tests/e2e-sub && python3 ../../tests/proxy_test.py --skip-ancestry) || fail=1  # ancestor match: boot (tests/) outranks repo-root sessions
rmdir tests/e2e-sub

# terminal-ancestry match: launch the e2e client from INSIDE the boot nvim
# (remote jobstart -> the shell is the boot session's process child) from an
# unrelated cwd -> must connect to the boot session without any cwd relation
REPO="$PWD"
rm -f tests/.ancestry.out
nvim --server "$SOCK" --remote-expr "jobstart(['/bin/sh','-c','cd /tmp && python3 $REPO/tests/proxy_test.py > $REPO/tests/.ancestry.out 2>&1'])" >/dev/null
ANCESTRY_OK=0
for _ in $(seq 1 150); do
	if [ -s tests/.ancestry.out ] && grep -q "proxy tests:" tests/.ancestry.out; then
		ANCESTRY_OK=1
		break
	fi
	sleep 0.2
done
if [ "$ANCESTRY_OK" = 1 ] && grep -q "all passed" tests/.ancestry.out; then
	echo "PASS terminal-ancestry e2e (client child of boot nvim, unrelated cwd)"
else
	echo "FAIL terminal-ancestry e2e: $(tail -3 tests/.ancestry.out 2>/dev/null)"
	fail=1
fi
rm -f tests/.ancestry.out

# wait/retry: client starts with NO sessions file yet; file appears mid-wait;
# parked tools/list must connect after discovery succeeds. Then a second run
# with no file at all must surface the waited-error (timeout path).
REPO="$PWD" FB_DIR="$REPO/tests/.wait-e2e" python3 - "$REPO" "$BOOTPID" "$SOCK" <<'EOF' || fail=1
import json, os, subprocess, sys, threading, time, tempfile
repo, bootpid, sock = sys.argv[1], sys.argv[2], sys.argv[3]
fb = tempfile.mkdtemp(prefix="nvimcp-wait-e2e.")
n_fail = [0]

def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else ": " + str(detail)[:200]))
    if not cond:
        n_fail[0] += 1

late_path = os.path.join(fb, "late-sessions.json")
p = subprocess.Popen(
    [sys.executable, repo + "/proxy/nvimcp-proxy.py",
     "--sessions-file", late_path, "--wait", "8", "--pid", bootpid],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE)

def send(msg):
    p.stdin.write((json.dumps(msg) + "\n").encode()); p.stdin.flush()
    return json.loads(p.stdout.readline())

def driver():
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    resp = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = [x.get("name") for x in resp.get("result", {}).get("tools", [])]
    check("late-arrival tools/list succeeded after retry",
          "result" in resp and "buffer_read" in names, resp)

t = threading.Timer(25, p.kill); t.start()
thread = threading.Thread(target=driver, daemon=True)
thread.start()
time.sleep(2.5)
with open(late_path, "w") as f:
    json.dump({"99991": {"pid": int(bootpid), "socket": sock, "cwd": "/tmp/unrelated",
                          "registered_at": 1}}, f)
thread.join(20)
p.stdin.close(); p.wait(timeout=5); t.cancel()
os.unlink(late_path)
os.rmdir(fb)

# timeout path: no file ever appears; error must mention the waited window
p2 = subprocess.Popen(
    [sys.executable, repo + "/proxy/nvimcp-proxy.py",
     "--sessions-file", os.path.join(fb, "missing.json"), "--wait", "2"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE)
p2.stdin.write((json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}}) + "\n").encode())
p2.stdin.flush()
line = json.loads(p2.stdout.readline())
err = line.get("error", {}).get("message", "")
check("waited-error surfaces on timeout", "waited" in err and "no nvimcp session" in err, err)
p2.stdin.close(); p2.wait(timeout=5)

sys.exit(1 if n_fail[0] else 0)
EOF

# fallback scenarios run from a cwd unrelated to any live session (mktemp)
REPO="$PWD"
FB_DIR=$(mktemp -d "${TMPDIR:-/tmp}/nvimcp-e2e-fb.XXXXXX")
trap 'rm -rf "$FB_DIR" tests/.fallback-sessions.json tests/.fallback-strict.json' EXIT

# recency/opt-in fallback: dead entry + boot entry marked newest with an
# unrelated cwd + fallback_recent=true -> must pick the boot session via opt-in
DEAD_PID=$(python3 -c 'import subprocess; p = subprocess.Popen(["true"]); p.wait(); print(p.pid)')
cat > tests/.fallback-sessions.json <<EOF
{"$DEAD_PID": {"pid": $DEAD_PID, "socket": "/nonexistent-nvimcp-e2e.sock", "cwd": "/tmp", "registered_at": 1000000},
 "99994": {"pid": $BOOTPID, "socket": "$SOCK", "cwd": "/tmp/unrelated-e2e-dir", "registered_at": 2000000, "fallback_recent": true}}
EOF
(cd "$FB_DIR" && python3 "$REPO/tests/proxy_test.py" --sessions-file "$REPO/tests/.fallback-sessions.json") || fail=1

# strict default: two live entries (boot pid, distinct sockets), no flag on
# either -> must refuse with an actionable message (no silent auto-connect)
cat > tests/.fallback-strict.json <<EOF
{"99993": {"pid": $BOOTPID, "socket": "/nonexistent-e2e-b.sock", "cwd": "/tmp/a", "registered_at": 1000000},
 "99994": {"pid": $BOOTPID, "socket": "$SOCK", "cwd": "/tmp/b", "registered_at": 2000000}}
EOF
STRICT_OUT=$(cd "$FB_DIR" && printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n' | python3 "$REPO/proxy/nvimcp-proxy.py" --sessions-file "$REPO/tests/.fallback-strict.json" 2>&1)
if printf '%s' "$STRICT_OUT" | grep -q "fallback_recent"; then
	echo "PASS strict no-match refuses without opt-in"
else
	echo "FAIL strict no-match refuses without opt-in: $STRICT_OUT"
	fail=1
fi
trap - EXIT
rm -rf "$FB_DIR" tests/.fallback-sessions.json tests/.fallback-strict.json

echo done > tests/.stop
wait "$bootpid" 2>/dev/null || true
trap - EXIT
exit $fail
