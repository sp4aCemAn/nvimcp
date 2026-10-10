local root = vim.fn.getcwd()
vim.opt.rtp:prepend(root)
package.path = root .. "/lua/?.lua;" .. root .. "/lua/?/init.lua;" .. package.path

local n_fail = 0
local function fail(name, msg)
	vim.notify(("FAIL %s: %s"):format(name, msg), vim.log.levels.ERROR)
	n_fail = n_fail + 1
end
local function pass(name)
	vim.notify("PASS " .. name, vim.log.levels.INFO)
end
local function ok(name, expr, msg)
	if expr then
		pass(name)
	else
		fail(name, msg or "assertion failed")
	end
end

local nvimcp = require("nvimcp")
local tools = require("nvimcp.tools")
local sanitize = require("nvimcp.sanitize")

-- works before setup() is ever called
local ok_list, pretools = pcall(function()
	return tools.list()
end)
ok("tools list before setup", ok_list and type(pretools) == "table" and #pretools >= 10, vim.inspect(ok_list))

-- status() shows tool names, not indices
local toolsline = table.concat(nvimcp.status(), "\n"):match("tools: [^\n]*")
ok("status lists tool names", toolsline ~= nil and toolsline:find("buffer_list") ~= nil and toolsline:find("lsp_diagnostics") ~= nil and toolsline:match("^tools: %a") ~= nil, vim.inspect(toolsline))

-- status()/proxy_path() resolve the installed proxy path from the runtime path
local proxyline = table.concat(nvimcp.status(), "\n"):match("proxy: [^\n]*")
ok("status shows proxy path", proxyline ~= nil and proxyline:match("nvimcp%-proxy%.py") ~= nil, vim.inspect(proxyline))
local ppath = nvimcp.proxy_path()
ok("proxy_path resolves to a real file", ppath ~= nil and ppath:match("nvimcp%-proxy%.py$") ~= nil and vim.uv.fs_stat(ppath) ~= nil, tostring(ppath))

nvimcp.setup({ autostart = false })
local cfg = require("nvimcp.config").options
ok("config defaults correct", cfg.autostart == false and cfg.allow_edit == true and cfg.rate_limit.calls == 10 and cfg.terminals.enabled == false)

-- sanitization: bare keys, not just key=value context
ok("scrub bare aws key", sanitize.scrub("value AKIAABCDEFGHIJKLMNOP here"):match("AKIA") == nil, sanitize.scrub("value AKIAABCDEFGHIJKLMNOP here"))
ok("scrub bare gcp key", sanitize.scrub("AIzaSyD1234567890abcdefghijklmnopqrstuv"):match("AIza") == nil, sanitize.scrub("AIzaSyD1234567890abcdefghijklmnopqrstuv"))
ok("scrub hex run", sanitize.scrub(string.rep("deadbeef", 16)):match("REDACTED") ~= nil)
ok("scrub gh token", sanitize.scrub("gho_abcdefghij1234567890abcdefghij1234"):match("gho_") == nil, sanitize.scrub("gho_abcdefghij1234567890abcdefghij1234"))

ok("start", nvimcp.start() and nvimcp.is_running())
local sock = require("nvimcp.server").socket_path()
ok("socket exists", sock and vim.uv.fs_stat(sock) ~= nil, "no socket at " .. tostring(sock))
local perms = vim.fn.getfperm(sock)
ok("socket perms", perms == "rw-------", "perms " .. perms)
ok("sessions file exists", vim.uv.fs_stat(require("nvimcp.server").sessions_path()) ~= nil)

local listed = tools.list()
ok("tools listed", #listed >= 10, "only " .. #listed)
for _, expected in ipairs({ "buffer_list", "buffer_info", "buffer_read", "buffer_edit", "buffer_search", "lsp_clients", "lsp_diagnostics", "lsp_definition", "lsp_references", "lsp_hover" }) do
	local found = false
	for _, t in ipairs(listed) do
		if t.name == expected then found = true end
	end
	ok("tool present: " .. expected, found)
end

local scratch = vim.api.nvim_create_buf(true, false)
vim.api.nvim_buf_set_lines(scratch, 0, -1, false, {
	"local api_key = 'AKIAABCDEFGHIJKLMNOP'",
	"local password = 'hunter2'",
	"... (+abc",
	"",
	"function M.foo()",
	"  return 'password = real-secret-value'",
	"end",
	"",
	"line with needle here",
	"another needle line",
})
vim.api.nvim_buf_set_name(scratch, "/tmp/nvimcp-test.lua")
vim.api.nvim_set_current_buf(scratch)
vim.bo[scratch].filetype = "lua"

local res
res = select(1, tools.call("buffer_list", {}))
ok("buffer_list finds scratch", vim.iter(res.buffers):any(function(b) return b.id == scratch end))

-- nvim resolves the buffer-name path (macOS maps /tmp -> /private/tmp), so
-- assert on the tail of the name rather than an exact string
res = select(1, tools.call("buffer_info", { buffer_id = scratch }))
ok("buffer_info", res.cursor ~= nil and res.line_count == 10 and res.name:match("nvimcp%-test%.lua$") ~= nil, vim.inspect({ c = res.cursor, l = res.line_count, n = res.name }))

res = select(1, tools.call("buffer_read", { buffer_id = scratch, start_line = 1, max_lines = 4 }))
ok("buffer_read scrub", not res.lines[1]:match("AKIA") and res.lines[1]:match("REDACTED"), res.lines[1])
ok("buffer_read scrub keyval", res.lines[2]:match("REDACTED") ~= nil, res.lines[2])
ok("buffer_read truncated flag", res.truncated == true, tostring(res.truncated))

res = select(1, tools.call("buffer_read", { buffer_id = scratch, start_line = 5, end_line = 5 }))
ok("buffer_read range", res.lines[1] == "function M.foo()", res.lines[1])

res = select(1, tools.call("buffer_search", { buffer_id = scratch, query = "needle" }))
ok("buffer_search literal", res.count == 2 and res.matches[1].line_number == 9, "count " .. res.count)

res = select(1, tools.call("buffer_search", { buffer_id = scratch, query = "M%.", regex = true }))
ok("buffer_search regex", res.count == 1 and res.matches[1].line_number == 5, "count " .. res.count)

local many = vim.api.nvim_create_buf(true, false)
local many_lines = {}
for i = 1, 250 do
	many_lines[i] = "needle " .. i
end
vim.api.nvim_buf_set_lines(many, 0, -1, false, many_lines)
res = select(1, tools.call("buffer_search", { buffer_id = many, query = "needle" }))
ok("buffer_search caps at 200", res.count == 200 and res.truncated == true, "count " .. tostring(res.count) .. " truncated " .. tostring(res.truncated))
res = select(1, tools.call("buffer_search", { buffer_id = many, query = "nonexistent-zzz" }))
ok("buffer_search no match not truncated", res.count == 0 and res.truncated == false, vim.inspect(res))

-- edits
vim.bo[scratch].modified = false
res = select(1, tools.call("buffer_edit", { buffer_id = scratch, start_line = 9, end_line = 9, text = "edited needle" }))
ok("buffer_edit", res ~= nil and vim.api.nvim_buf_get_lines(scratch, 8, 9, false)[1] == "edited needle", vim.inspect(vim.api.nvim_buf_get_lines(scratch, 7, 10, false)))
ok("buffer_edit replaced echo", res.replaced.start == 9 and res.replaced.stop == 9 and res.line_count == 10, vim.inspect(res.replaced))

-- append via end_line > line count
vim.bo[scratch].modified = false
res = select(1, tools.call("buffer_edit", { buffer_id = scratch, start_line = 11, end_line = 20, text = "appended via api" }))
ok("buffer_edit append", vim.api.nvim_buf_get_lines(scratch, 10, 11, false)[1] == "appended via api" and res.line_count == 11, vim.inspect(res))

-- readonly / modifiable guards
local b2 = vim.api.nvim_create_buf(false, false)
vim.api.nvim_buf_set_lines(b2, 0, -1, false, { "a" })
vim.bo[b2].modified = false
vim.bo[b2].modifiable = false
local _, err = tools.call("buffer_edit", { buffer_id = b2, start_line = 1, end_line = 1, text = "x", force = true })
ok("buffer_edit modifiable guard", err ~= nil and err:match("modifiable"), tostring(err))

vim.bo[scratch].readonly = true
vim.bo[scratch].modified = false
_, err = tools.call("buffer_edit", { buffer_id = scratch, start_line = 1, end_line = 1, text = "y" })
ok("buffer_edit readonly guard", err ~= nil and err:match("readonly"), tostring(err))
res = select(1, tools.call("buffer_edit", { buffer_id = scratch, start_line = 1, end_line = 1, text = "x", force = true }))
ok("buffer_edit readonly force works", res ~= nil, tostring(res))
vim.bo[scratch].readonly = false

vim.bo[scratch].modified = true
_, err = tools.call("buffer_edit", { buffer_id = scratch, start_line = 1, end_line = 1, text = "z" })
ok("buffer_edit force guard", err ~= nil and err:match("unsaved"), tostring(err))
res = select(1, tools.call("buffer_edit", { buffer_id = scratch, start_line = 1, end_line = 1, text = "x", force = true }))
ok("buffer_edit force works", res ~= nil)

vim.bo[scratch].modified = false
_, err = tools.call("buffer_edit", { buffer_id = scratch, start_line = 0, end_line = 1, text = "x" })
ok("buffer_edit start_line guard", err ~= nil and err:match("1%-based"), tostring(err))
_, err = tools.call("buffer_edit", { buffer_id = scratch, start_line = 2, end_line = 1, text = "x" })
ok("buffer_edit end_line guard", err ~= nil and err:match("end_line"), tostring(err))
_, err = tools.call("buffer_edit", { buffer_id = scratch, start_line = 1, end_line = 1 })
ok("buffer_edit text guard", err ~= nil and err:match("text"), tostring(err))

-- diagnostics severity filtering with real vim.diagnostic data
local ns = vim.api.nvim_create_namespace("nvimcp_test_ns")
vim.diagnostic.set(ns, scratch, {
	{ lnum = 0, col = 0, message = "one bad thing", severity = vim.diagnostic.severity.ERROR },
	{ lnum = 1, col = 0, message = "two bad thing", severity = vim.diagnostic.severity.WARN },
	{ lnum = 2, col = 0, message = "three bad thing", severity = vim.diagnostic.severity.HINT },
}, {})
local d1 = select(1, tools.call("lsp_diagnostics", { buffer_id = scratch, severity_max = 1 }))
ok("severity_max 1 errors only", d1.count == 1 and d1.truncated == false and d1.diagnostics[1].severity == "ERROR" and #d1.diagnostics == 1, vim.inspect(d1))
local d2 = select(1, tools.call("lsp_diagnostics", { buffer_id = scratch, severity_max = 2 }))
ok("severity_max 2 err+warn", d2.count == 2 and #d2.diagnostics == 2, vim.inspect(d2))
local d4 = select(1, tools.call("lsp_diagnostics", { buffer_id = scratch, severity_max = 4 }))
ok("severity_max 4 all", d4.count == 3 and #d4.diagnostics == 3, vim.inspect(d4))
ok("diagnostics message scrubbed", d1.diagnostics[1].message == "one bad thing", d1.diagnostics[1].message)

res, err = tools.call("lsp_clients", {})
ok("lsp_clients no error", err == nil, tostring(err))
ok("lsp_clients empty is fine", type(res) == "table")

res, err = tools.call("lsp_diagnostics", {})
ok("lsp_diagnostics no error", err == nil, tostring(err))

-- stub an attached LSP to regression-test hover/definition handlers
local orig_req = vim.lsp.buf_request_sync
local restore_req = function()
	vim.lsp.buf_request_sync = orig_req
end

vim.lsp.buf_request_sync = function()
	return { { result = { contents = { value = "docs for ApiKey with api_key = topsecretval" } } } }
end
local h, herr = tools.call("lsp_hover", {})
restore_req()
ok("hover via stubbed LSP", herr == nil and h.hover ~= nil and h.hover:match("docs for ApiKey") ~= nil and h.hover:match("REDACTED") ~= nil, vim.inspect(h, herr))

vim.lsp.buf_request_sync = function()
	local rng = { start = { line = 2, character = 4 }, endEnd = nil }
	return { { result = { { uri = "file:///tmp/nvimcp-nope.lua", range = rng } } } }
end
local dfn, dferr = tools.call("lsp_definition", {})
restore_req()
ok("definition via stubbed LSP", dferr == nil and dfn.definitions[1] ~= nil and dfn.definitions[1].path == "/tmp/nvimcp-nope.lua" and dfn.definitions[1].line == 3, vim.inspect(dfn, dferr))

local refs, rerr = nil, nil
vim.lsp.buf_request_sync = function()
	return { { result = {
		{ uri = "file:///tmp/nvimcp-test.lua", range = { start = { line = 0, character = 0 } } },
		{ uri = "file:///tmp/nvimcp-test.lua", range = { start = { line = 4, character = 0 } } },
	} } }
end
refs, rerr = tools.call("lsp_references", {})
restore_req()
ok("references via stubbed LSP", rerr == nil and refs.count == 2 and refs.references[2].line == 5, vim.inspect(refs, rerr))

-- partial position args: a given line is honored even without a character
vim.lsp.buf_request_sync = function(_, _, params)
	return { { result = { contents = { value = ("pos %d:%d"):format(params.position.line, params.position.character) } } } }
end
local pres, perr = tools.call("lsp_hover", { buffer_id = scratch, line = 5 })
restore_req()
ok("partial position uses given line", perr == nil and pres.hover:match("pos 4:0") ~= nil, vim.inspect(pres, perr))

vim.lsp.buf_request_sync = function(_, _, params)
	return { { result = { contents = { value = ("pos %d:%d"):format(params.position.line, params.position.character) } } } }
end
local pres2, perr2 = tools.call("lsp_hover", { buffer_id = scratch, character = 2 })
restore_req()
ok("character-only uses cursor line", perr2 == nil and pres2.hover:match("pos %d+:2") ~= nil, vim.inspect(pres2, perr2))

-- rate limiting / dispatch
require("nvimcp.config").options.rate_limit = { calls = 2, window = 60 }
local server = require("nvimcp.server")
server.rpc("call_tool", { name = "buffer_list" })
server.rpc("call_tool", { name = "buffer_list" })
res = server.rpc("call_tool", { name = "buffer_list" })
ok("rate limit", res.error ~= nil, vim.inspect(res))
require("nvimcp.config").options.rate_limit = { calls = 10, window = 5 }

res = server.rpc(" bogus ", {})
ok("unknown method", res.error ~= nil)

res = server.rpc("call_tool", { name = "nonexistent_tool" })
ok("unknown tool", res.error ~= nil)

-- disable via config
require("nvimcp.config").options.disable = { "buffer_edit" }
res, err = tools.call("buffer_edit", {})
ok("disabled tool blocks call", err ~= nil, "expected error")
require("nvimcp.config").options.disable = {}

-- allow_edit=false unregisters the tool entirely (requires registry reload)
local clear_tools_loaded = function()
	package.loaded["nvimcp.tools"] = nil
	package.loaded["nvimcp.tools.buffer"] = nil
	package.loaded["nvimcp.tools.lsp"] = nil
end
cfg.allow_edit = false
clear_tools_loaded()
local listed_edit = require("nvimcp.tools").list()
local has_edit = false
for _, t in ipairs(listed_edit) do
	if t.name == "buffer_edit" then has_edit = true end
end
ok("allow_edit=false unregisters", not has_edit)
cfg.allow_edit = true
clear_tools_loaded()
listed_edit = require("nvimcp.tools").list()
has_edit = false
for _, t in ipairs(listed_edit) do
	if t.name == "buffer_edit" then has_edit = true end
end
ok("allow_edit=true registers", has_edit)

-- terminals.enabled without the phase-3 module must not break the registry
require("nvimcp.config").options.terminals.enabled = true
clear_tools_loaded()
local term_ok, term_list = pcall(function()
	return require("nvimcp.tools").list()
end)
ok("terminals.enabled tolerates missing term module", term_ok and #term_list >= 10, tostring(term_ok) .. " " .. vim.inspect(term_list and #term_list))
require("nvimcp.config").options.terminals.enabled = false
clear_tools_loaded()
require("nvimcp.tools").list()

-- stale sessions entries (dead socket) are pruned on next start
local sessions_file = server.sessions_path()
local okdec, sdata = pcall(vim.json.decode, table.concat(vim.fn.readfile(sessions_file), "\n"))
if okdec and type(sdata) == "table" then
	sdata["999999999999"] = { pid = 999999999999, socket = "/nonexistent-nvimcp-test.sock", cwd = "/tmp", config = "/tmp" }
	vim.fn.writefile({ vim.json.encode(sdata) }, sessions_file)
	nvimcp.stop()
	nvimcp.start()
	local okdec2, after = pcall(vim.json.decode, table.concat(vim.fn.readfile(sessions_file), "\n"))
	ok("stale sessions entry pruned", okdec2 and after["999999999999"] == nil, vim.inspect(after))
	ok("own sessions entry kept", okdec2 and after[tostring(vim.fn.getpid())] ~= nil, vim.inspect(after))
	ok("sessions tmp file cleaned", vim.uv.fs_stat(sessions_file .. "." .. vim.fn.getpid() .. ".tmp") == nil)
else
	fail("prune setup", "could not read sessions file")
end

-- rpc on a stopped server reports "not started" without burning rate slots
require("nvimcp.config").options.rate_limit = { calls = 2, window = 1 }
nvimcp.stop()
res = server.rpc("call_tool", { name = "buffer_list" })
ok("stopped rpc reports not started", res.error ~= nil and res.error:match("not started") ~= nil, vim.inspect(res))
nvimcp.start()
local r1 = server.rpc("call_tool", { name = "buffer_list" })
local r2 = server.rpc("call_tool", { name = "buffer_list" })
local r3 = server.rpc("call_tool", { name = "buffer_list" })
ok("stopped calls do not burn rate slots", r1.error == nil and r2.error == nil and r3.error ~= nil, vim.inspect({ r1.error, r2.error, r3.error }))
require("nvimcp.config").options.rate_limit = { calls = 10, window = 5 }

-- status pane: q must close the floating window
if not vim.api.nvim_get_commands({})["NvimcpStatus"] then
	vim.cmd("source " .. root .. "/plugin/nvimcp.lua")
end
vim.cmd("NvimcpStatus")
local status_win = vim.api.nvim_get_current_win()
vim.api.nvim_feedkeys("q", "mx", false)
vim.cmd("redraw")
ok("status pane q closes window", not vim.api.nvim_win_is_valid(status_win))

ok("stop", nvimcp.stop() and not nvimcp.is_running())
ok("socket removed", vim.uv.fs_stat(sock) == nil)

if n_fail > 0 then
	vim.notify(("nvimcp tests: %d failures"):format(n_fail), vim.log.levels.ERROR)
	os.exit(1)
end
vim.notify("nvimcp tests: all passed", vim.log.levels.INFO)
