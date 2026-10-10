#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

fail=0
check() {
	if [ "$1" = "$2" ]; then
		echo "PASS $3"
	else
		echo "FAIL $3: got '$1' want '$2'"
		fail=1
	fi
}

NVIM=$(command -v nvim)
Q="'"
"$NVIM" --headless -l tests/boot.lua &
bootpid=$!
trap 'touch tests/.stop; wait "$bootpid" 2>/dev/null || true' EXIT

for _ in $(seq 1 50); do
	[ -s tests/.boot.sock ] && break
	sleep 0.2
done
SOCK=$(cat tests/.boot.sock)
echo "target socket: $SOCK"

N() {
	"$NVIM" --server "$SOCK" --remote-expr "$1"
}

check "$(N "luaeval(\"tostring(vim.tbl_count(require(${Q}nvimcp.tools${Q}).list()) >= 10)\")")" \
	"true" "cross-process: tools/list" || true

check "$(N "luaeval(\"tostring(require(${Q}nvimcp.server${Q}).rpc(${Q}call_tool${Q}, {name=${Q}buffer_read${Q}, arguments={}}).result.lines[1])\")")" \
	"original line one" "cross-process: buffer_read" || true

check "$(N "luaeval(\"tostring(require(${Q}nvimcp.server${Q}).rpc(${Q}call_tool${Q}, {name=${Q}buffer_edit${Q}, arguments={start_line=2, end_line=2, text=${Q}edited over rpc${Q}}}).error == nil)\")")" \
	"true" "cross-process: buffer_edit" || true

result1=$(N "luaeval(\"(function() local ok, res = pcall(vim.api.nvim_buf_get_lines, 0, 1, 2, false) if ok then return res[1] end return 'ERR' end)()\")")
check "$result1" "edited over rpc" "cross-process: edit visible" || true

check "$(N "luaeval(\"tostring(require(${Q}nvimcp.server${Q}).rpc(${Q}call_tool${Q}, {name=${Q}buffer_edit${Q}, arguments={start_line=1, end_line=1, text=${Q}x${Q}}}) ~= nil)\")")" \
	"true" "cross-process: force guard" || true

check "$(N "luaeval(\"tostring(require(${Q}nvimcp.server${Q}).rpc(${Q}nonexistent${Q}).error ~= nil)\")")" \
	"true" "cross-process: unknown method" || true

check "$(N "luaeval(\"require(${Q}nvimcp.server${Q}).socket_path()\")")" \
	"$SOCK" "cross-process: socket path matches" || true

echo done > tests/.stop
wait "$bootpid" 2>/dev/null || true
trap - EXIT

exit $fail
