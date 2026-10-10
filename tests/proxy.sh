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

# recency fallback: unrelated cwd + a dead entry + the (alive) boot entry marked
# newest with an unrelated cwd -> must pick the boot session, not error
mkdir -p tests/e2e-fallback
DEAD_PID=$(python3 -c 'import subprocess; p = subprocess.Popen(["true"]); p.wait(); print(p.pid)')
cat > tests/.fallback-sessions.json <<EOF
{"$DEAD_PID": {"pid": $DEAD_PID, "socket": "/nonexistent-nvimcp-e2e.sock", "cwd": "/tmp", "registered_at": 1000000},
 "99994": {"pid": $BOOTPID, "socket": "$SOCK", "cwd": "/tmp/unrelated-e2e-dir", "registered_at": 2000000}}
EOF
(cd tests/e2e-fallback && python3 ../../tests/proxy_test.py --sessions-file ../.fallback-sessions.json) || fail=1
rm -f tests/.fallback-sessions.json tests/.fallback-sessions.json.*.tmp
rmdir tests/e2e-fallback

echo done > tests/.stop
wait "$bootpid" 2>/dev/null || true
trap - EXIT
exit $fail
