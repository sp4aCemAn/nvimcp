local sanitize = require("nvimcp.sanitize")
local config = require("nvimcp.config")
local tools = require("nvimcp.tools")

local M = {}

-- Resolve the nvimterminals plugin. The API may live on the init module or
-- the terminals submodule depending on version; prefer whichever exposes it.
-- The tools degrade gracefully: term_list/term_read work with the plain
-- terminal API, term_send additionally requires the agent-control surface.
local function terminals_mod()
	local ok, init = pcall(require, "nvimterminals")
	if not ok or type(init) ~= "table" then
		return nil
	end
	local has_surface = function(t)
		return type(t) == "table"
			and (type(t.terminals) == "table"
				or type(t.send) == "function"
				or type(t.lines) == "function"
				or type(t.get) == "function")
	end
	if has_surface(init) then
		return init
	end
	local ok2, sub = pcall(require, "nvimterminals.terminals")
	if has_surface(sub) then
		return sub
	end
	return nil
end

local function output_cap(requested)
	local max = config.options.max_output_lines or 500
	return math.min(requested and math.floor(requested) or max, max)
end

local function state_for(t, name)
	if type(t.get) == "function" then
		local ok, st = pcall(t.get, name)
		if ok then
			return st
		end
	end
	if type(t.terminals) == "table" then
		return t.terminals[name]
	end
	return nil
end

local function required_name(args)
	local name = args and args.name
	if type(name) ~= "string" or name == "" then
		error("name is required and must be a non-empty string")
	end
	return name
end

local function register_term_list()
	tools.register({
		name = "term_list",
		description = "List terminals managed by nvimterminals in this session (name, running/dead, pid, exit code, agent permission, line count).",
		inputSchema = {
			type = "object",
			properties = {},
		},
		handler = function()
			local t = terminals_mod()
			if not t then
				error("nvimterminals is not available in this session")
			end
			local out = {}
			for _, name in ipairs(t.list() or {}) do
				local entry = { name = name }
				local st = state_for(t, name)
				if st ~= nil then
					entry.buf = st.buf
					entry.pid = st.pid
					entry.dead = st.dead and true or false
					entry.exit_code = st.exit_code
					if type(t.agent_enabled) == "function" then
						local ok, enabled = pcall(t.agent_enabled, name)
						entry.agent_enabled = ok and enabled or false
					elseif st.agent_enabled ~= nil then
						entry.agent_enabled = st.agent_enabled and true or false
					end
					entry.running = not entry.dead
					if entry.buf and vim.api.nvim_buf_is_valid(entry.buf) then
						entry.line_count = vim.api.nvim_buf_line_count(entry.buf)
					end
				end
				table.insert(out, entry)
			end
			return { terminals = out, count = #out }
		end,
	})
end

local function register_term_read()
	tools.register({
		name = "term_read",
		description = "Read the last n lines of a terminal's scrollback (name from term_list). Output is always scrubbed for secrets and capped by the server's max_output_lines setting. Works on running and exited terminals.",
		inputSchema = {
			type = "object",
			properties = {
				name = { type = "string", description = "Terminal name (from term_list)" },
				n = { type = "integer", description = "Number of trailing lines to return (defaults to max_output_lines)" },
			},
			required = { "name" },
		},
		handler = function(args)
			local t = terminals_mod()
			if not t then
				error("nvimterminals is not available in this session")
			end
			local name = required_name(args)
			local n = output_cap(args and args.n)
			local lines, line_range, total_lines, running, dead
			if type(t.lines) == "function" then
				local ok, res = pcall(t.lines, name, { n = n })
				if not ok or type(res) ~= "table" then
					error("reading terminal '" .. name .. "' failed: " .. tostring(res))
				end
				lines = res.lines or {}
				if type(lines[1]) == "table" then
					-- newer nvimterminals: lines = { { lnum, text } }
					local strs = {}
					for _, l in ipairs(lines) do
						strs[#strs + 1] = l.text or ""
					end
					lines = strs
				end
				total_lines = res.total_lines or res.total
				line_range = res.line_range
					or { start = res.start, stop = res["end"] or res.stop }
				if res.running ~= nil then
					running = res.running
				elseif res.dead ~= nil then
					running = not res.dead
				end
				dead = res.dead
			else
				-- Fallback for older nvimterminals: read the terminal buffer directly.
				local st = state_for(t, name)
				if not st or not (st.buf and vim.api.nvim_buf_is_valid(st.buf)) then
					error("no such terminal: " .. tostring(name))
				end
				total_lines = vim.api.nvim_buf_line_count(st.buf)
				local start = math.max(1, total_lines - n + 1)
				lines = vim.api.nvim_buf_get_lines(st.buf, start - 1, total_lines, false)
				line_range = { start = start, stop = total_lines }
				running = vim.fn.jobwait({ st.job_id }, 0)[1] == -1
			end
			local out, _, truncated = sanitize.truncate_table(sanitize.scrub_lines(lines), n)
			local pid, exit_code
			local st = state_for(t, name)
			if st ~= nil then
				pid = st.pid
				exit_code = st.exit_code
				if running == nil then
					if st.dead ~= nil then
						running = not st.dead
					end
				end
				if st.job_id and st.pid == nil then
					local okp, p = pcall(vim.fn.jobpid, st.job_id)
					if okp then
						pid = p
					end
				end
			end
			return {
				name = name,
				running = running,
				dead = dead,
				pid = pid,
				exit_code = exit_code,
				total_lines = total_lines,
				line_range = line_range,
				lines = out,
				truncated = truncated,
			}
		end,
	})
end

local function register_term_send()
	tools.register({
		name = "term_send",
		description = "Send a line of input to a terminal (appends a newline if missing). Authorization is defined entirely by nvimterminals: the terminal must have agent control enabled (:TermAgent) and its command policy must allow the line (:TermPolicy). Denials are returned as tool errors with the reason string.",
		inputSchema = {
			type = "object",
			properties = {
				name = { type = "string", description = "Terminal name (from term_list)" },
				data = { type = "string", description = "Line to send; a trailing newline is added unless present" },
			},
			required = { "name", "data" },
		},
		handler = function(args)
			local t = terminals_mod()
			if not t then
				error("nvimterminals is not available in this session")
			end
			if type(t.send) ~= "function" then
				error("term_send requires an nvimterminals version with agent control (M.send); update the plugin")
			end
			local name = required_name(args)
			local data = args and args.data
			if type(data) ~= "string" or data == "" then
				error("data is required and must be a non-empty string")
			end
			if data:sub(-1) ~= "\n" and data:sub(-1) ~= "\r" then
				data = data .. "\n"
			end
			local ok, res = pcall(t.send, name, data, { lstrip = false })
			if not ok or type(res) ~= "table" then
				error("sending to terminal '" .. name .. "' failed: " .. tostring(res))
			end
			if res.ok then
				return { sent = true, name = name, bytes = #data }
			end
			-- Denial: surface the authorization reason verbatim as a tool error.
			error(("term_send denied (%s)%s"):format(res.reason or "unknown", res.detail and (": " .. tostring(res.detail)) or ""))
		end,
	})
end

function M.register()
	local t = terminals_mod()
	if not t then
		vim.notify(
			"[nvimcp] terminals.enabled is set but nvimterminals is not loaded — term tools not registered",
			vim.log.levels.WARN
		)
		return false
	end
	register_term_list()
	register_term_read()
	if type(t.send) == "function" then
		register_term_send()
	else
		vim.notify(
			"[nvimcp] nvimterminals lacks the agent-control API (M.send) — term_send not registered; update the plugin",
			vim.log.levels.WARN
		)
	end
	return true
end

return M
