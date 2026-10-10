-- Headless e2e for nvimcp term_* tools over the live nvimterminals plugin.
-- Run from the nvimcp repo root:
--   nvim --headless -l tests/terminal.lua
-- Optional: NVIMTERMINALS_PATH=/path/to/nvimterminals to test a dev checkout.

local root = vim.fn.getcwd()
vim.opt.rtp:prepend(root)
package.path = root .. "/lua/?.lua;" .. root .. "/lua/?/init.lua;" .. package.path

local n_fail = 0
local function pass(name)
	print("PASS " .. name)
end
local function fail(name, msg)
	print("FAIL " .. name .. ": " .. tostring(msg))
	n_fail = n_fail + 1
end
local function ok(name, expr, msg)
	if expr then
		pass(name)
	else
		fail(name, msg or "assertion failed")
	end
end

local tpath = os.getenv("NVIMTERMINALS_PATH")
if tpath and vim.fn.isdirectory(tpath) == 1 then
	vim.opt.rtp:append(tpath)
end

local tools = require("nvimcp.tools")
local term_tools = require("nvimcp.tools.term")

local has_terminals, terminals_init = pcall(require, "nvimterminals")
if not has_terminals or type(terminals_init) ~= "table" then
	print("SKIP: nvimterminals is not on the runtimepath (install it or set NVIMTERMINALS_PATH)")
	os.exit(0)
end
-- The API may live on the init module or the terminals submodule depending
-- on version; resolve whichever exposes the terminal surface.
local terminals = terminals_init
if type(terminals.toggle) ~= "function" and type(terminals.send) ~= "function" then
	local ok_sub, sub = pcall(require, "nvimterminals.terminals")
	if ok_sub and type(sub) == "table" then
		terminals = sub
	end
end

-- Configure both plugins the way a real user would.
require("nvimterminals.config").setup({ terminal_cmd = "/bin/cat" })
local nvimcp = require("nvimcp")
nvimcp.setup({ autostart = false, terminals = { enabled = true } })

-- Registration: terminals.enabled=true + plugin present registers list/read always
local listed = tools.list()
local function has(name)
	for _, t in ipairs(listed) do
		if t.name == name then
			return true
		end
	end
	return false
end
ok("term_list registered", has("term_list"))
ok("term_read registered", has("term_read"))
local send_supported = type(terminals.send) == "function"
ok("term_send registered only when nvimterminals has agent control", has("term_send") == send_supported,
	"send_supported=" .. tostring(send_supported))
ok("rate-limit/buffer/lsp tools unaffected", has("buffer_list") and has("lsp_diagnostics"))

-- Gate: terminals.enabled=false removes term tools from the registry
local saw_term = false
nvimcp.setup({ autostart = false, terminals = { enabled = false } })
local tools_mod = require("nvimcp.tools")
tools_mod.loaded = false
tools_mod.registry = {}
for _, t in ipairs(tools_mod.list()) do
	if t.name:find("^term_") then
		saw_term = true
	end
end
ok("terminals.enabled=false unregisters term tools", not saw_term)

-- Re-enable and exercise the tools end-to-end.
nvimcp.setup({ autostart = false, terminals = { enabled = true } })
tools_mod.loaded = false
tools_mod.registry = {}

local res_list_empty = tools.call("term_list")
ok("term_list empty", res_list_empty and res_list_empty.count == 0, vim.inspect(res_list_empty))

-- Spawn a terminal through the nvimterminals public API (same path a user takes).
terminals.toggle("mcp-test")
local st = (type(terminals.get) == "function" and terminals.get("mcp-test")) or terminals.terminals["mcp-test"]
ok("terminal spawned with own buffer", st ~= nil and st.buf and vim.api.nvim_buf_is_valid(st.buf))

local res_list = tools.call("term_list")
ok("term_list sees spawned terminal", res_list and res_list.count == 1 and res_list.terminals[1].name == "mcp-test",
	vim.inspect(res_list))
ok("term_list reports running", res_list.terminals[1].running == true, vim.inspect(res_list.terminals[1]))
if send_supported then
	ok("term_list reports pid", type(res_list.terminals[1].pid) == "number")
end

-- Denial path: agent control is off by default; term_send must be denied.
if send_supported then
	local res, deny_err = tools.call("term_send", { name = "mcp-test", data = "echo hello" })
	ok("term_send denied when agent control is off", res == nil and deny_err ~= nil, tostring(deny_err))
	ok("denial reason is contract-shaped",
		tostring(deny_err):find("denied %(agent_disabled%)") ~= nil
			or tostring(deny_err):find("denied %(policy_denied%)") ~= nil,
		tostring(deny_err))
	local st3 = (terminals.get and terminals.get("mcp-test")) or (terminals.terminals or {})["mcp-test"]
	ok("denial keeps the terminal alive", st3 and st3.buf and vim.api.nvim_buf_is_valid(st3.buf))

	-- Enable agent control the way the user would: global config + per-term toggle.
	require("nvimterminals.config").setup({ terminal_cmd = "/bin/cat", agent = { enabled = true, allow = { "echo" } } })
	if type(terminals.agent_toggle) == "function" then
		terminals.agent_toggle("mcp-test")
		ok("agent_toggle results in enabled terminal", terminals.agent_enabled("mcp-test") == true)
	elseif terminals.terminals and terminals.terminals["mcp-test"] then
		terminals.terminals["mcp-test"].agent_enabled = true
	elseif terminals.get then
		local st2 = terminals.get("mcp-test")
		if st2 then
			st2.agent_enabled = true
		end
	end

	local sent, sent_err = tools.call("term_send", { name = "mcp-test", data = "echo hello" })
	ok("term_send allowed on enabled terminal", sent and sent.sent == true, vim.inspect(sent_err))

	-- Output actually landed in the pty and is readable through the MCP tool.
	-- (Read a wide window: the cat echo sits at the top of a static screen.)
	local text = nil
	for _ = 1, 20 do
		vim.wait(100)
		local res_read = tools.call("term_read", { name = "mcp-test", n = 50 })
		text = res_read and table.concat(res_read.lines or {}, "\n") or ""
		if text:find("echo hello", 1, true) then
			break
		end
	end
	ok("term_read sees the command output", #text > 0 and text:find("echo hello", 1, true) ~= nil, vim.inspect(text))

	-- Policy: a denied command returns a policy_denied tool error, terminal unharmed.
	local pol_res, pol_err = tools.call("term_send", { name = "mcp-test", data = "python3 -V" })
	ok("policy_denied surfaces as tool error",
		pol_res == nil and tostring(pol_err):find("policy_denied") ~= nil, tostring(pol_err))

	-- Unknown terminal names error cleanly, never touching the registry.
	local unk_res, unk_err = tools.call("term_send", { name = "nope", data = "hi" })
	ok("term_send unknown terminal",
		unk_res == nil and tostring(unk_err):find("no_such_terminal") ~= nil, tostring(unk_err))
else
	print("SKIP: agent-control surface (M.send) not present in this nvimterminals build")
	local res_read_once = tools.call("term_read", { name = "mcp-test", n = 10 })
	ok("term_read fallback works without agent API", res_read_once and type(res_read_once.lines) == "table",
		vim.inspect(res_read_once))
end

if n_fail > 0 then
	print(string.format("terminal tests: %d failures", n_fail))
	os.exit(1)
end
print("terminal tests: all passed")
os.exit(0)
