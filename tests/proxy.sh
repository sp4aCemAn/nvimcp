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
"$NVIM" --headless -l tests/boot.lua &
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
(cd tests && python3 ../tests/proxy_test.py) || fail=1        # exact cwd match -> boot session (tests/ cwd)
mkdir -p tests/e2e-sub
(cd tests/e2e-sub && python3 ../../tests/proxy_test.py) || fail=1  # ancestor match: boot (tests/) outranks repo-root sessions
rmdir tests/e2e-sub

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
