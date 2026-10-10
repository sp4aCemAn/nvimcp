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
python3 tests/proxy_test.py || fail=1   # default discovery: sessions file picked by cwd
(cd tests && python3 ../tests/proxy_test.py) || fail=1   # ancestor discovery: nvim at repo root, client in subdir

echo done > tests/.stop
wait "$bootpid" 2>/dev/null || true
trap - EXIT
exit $fail
