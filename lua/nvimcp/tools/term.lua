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
		description = "List terminals managed by nvimterminals in this session (name, running/dead, pid, exit code, agent permission, line count). Dead terminals keep their scrollback.",
		inputSchema = {
			type = "object",
			-- NOTE: properties must never be an empty table — empty Lua tables
			-- marshal across msgpack as JSON arrays, which MCP clients reject.
			properties = {
				include_dead = {
					type = "boolean",
					description = "Include exited terminals (default false)",
				},
			},
		},
		handler = function(args)
			local t = terminals_mod()
			if not t then
				error("nvimterminals is not available in this session")
			end
			local out = {}
			local include_dead = args and args.include_dead and true or false
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
				if entry.dead and not include_dead then
					goto continue
				end
				table.insert(out, entry)
				::continue::
			end
			return { terminals = out, count = #out }
		end,
	})
end

-- Read lines from a terminal, normalizing both nvimterminals shapes.
-- opts.n = count; opts.start = first buffer line (1-based) for ranged reads.
-- Returns { lines=str[], line_range, total_lines, running, dead, pid, exit_code }.
local function read_tail(t, name, opts)
  local n = opts and opts.n or 25
  local start = opts and opts.start or nil
  local lines, line_range, total_lines, running, dead
  if type(t.lines) == "function" then
    local ok, res = pcall(t.lines, name, start and { n = n, start = start } or { n = n })
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
    line_range = res.line_range or { start = res.start, stop = res["end"] or res.stop }
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
    local first = start or math.max(1, total_lines - n + 1)
    local last = math.min(total_lines, first + n - 1)
    lines = vim.api.nvim_buf_get_lines(st.buf, first - 1, last, false)
    line_range = { start = first, stop = last }
    running = vim.fn.jobwait({ st.job_id }, 0)[1] == -1
  end
  local pid, exit_code
  local st = state_for(t, name)
  if st ~= nil then
    pid = st.pid
    exit_code = st.exit_code
    if running == nil and st.dead ~= nil then
      running = not st.dead
    end
    if st.job_id and st.pid == nil then
      local okp, p = pcall(vim.fn.jobpid, st.job_id)
      if okp then
        pid = p
      end
    end
  end
  return {
    lines = lines,
    line_range = line_range,
    total_lines = total_lines,
    running = running,
    dead = dead,
    pid = pid,
    exit_code = exit_code,
  }
end

local function has_content(lines)
	for _, l in ipairs(lines) do
		if vim.trim(l or "") ~= "" then
			return true
		end
	end
	return false
end

-- Collapse runs of blank grid rows into annotated markers while keeping
-- first_line + index coordinates real. Terminal buffers are grid snapshots,
-- not transcripts — a quiet 66-row shell reads as 1 prompt + 65 blanks.
local function condense_lines(lines, first_line)
	first_line = first_line or 1
	local out, run = {}, 0
	for i, text in ipairs(lines) do
		if vim.trim(text or "") == "" then
			run = run + 1
		else
			if run > 0 then
				if run > 2 then
					local from, to = first_line + i - run - 1, first_line + i - 2
					table.insert(out, from == to
						and string.format("      ┆ row %d blank", from)
						or string.format("      ┆ rows %d-%d blank (%d lines)", from, to, run))
				else
					for _ = 1, run do table.insert(out, "") end
				end
				run = 0
			end
			table.insert(out, text)
		end
	end
	if run > 0 then
		local from = first_line + #lines - run
		if run > 2 then
			table.insert(out, string.format("      ┆ rows %d-%d blank to end of screen (%d lines)", from, from + run - 1, run))
		else
			for _ = 1, run do table.insert(out, "") end
		end
	end
	return out
end

-- Where the shell's cursor sits (next output lands there). Available only
-- while the terminal buffer is displayed in some window; nil when hidden or
-- errored. for the registered buffer state we defer to the caller.
local function term_cursor(buf)
	if not buf or not vim.api.nvim_buf_is_valid(buf) then
		return nil
	end
	for _, w in ipairs(vim.api.nvim_list_wins()) do
		local ok, wbuf = pcall(vim.api.nvim_win_get_buf, w)
		if ok and wbuf == buf then
			local okc, cur = pcall(vim.api.nvim_win_get_cursor, w)
			if okc then
				return { line = cur[1], col = cur[2] }
			end
		end
	end
	return nil
end

-- Read + post-process the lines requested: condense blank runs into span
-- markers unless raw requested; scrub always; cap always.
local function present_lines(r, n, condensed)
	local lines = r.lines or {}
	local first_line = (r.line_range and r.line_range.start) and r.line_range.start or 1
	local out_lines
	if condensed and has_content(lines) then
		out_lines = condense_lines(lines, first_line)
	else
		out_lines = sanitize.scrub_lines(lines)
	end
	local out, _, truncated = sanitize.truncate_table(out_lines, n)
	return out, truncated
end

local function register_term_read()
	tools.register({
		name = "term_read",
		description = "Read lines from a terminal's scrollback (name from term_list); reads the last n lines by default, or a [start_line, start_line+n) window with start_line. Blank runs are collapsed to span markers (condensed) — pass condensed=false to read the grid raw. Output is always scrubbed for secrets and capped by the server's max_output_lines setting. Works on running and exited terminals.",
		inputSchema = {
			type = "object",
			properties = {
				name = { type = "string", description = "Terminal name (from term_list)" },
				n = { type = "integer", description = "Number of lines to return (defaults to max_output_lines)" },
				start_line = {
					type = "integer",
					description = "1-based first buffer line; when given, reads start_line..start_line+n-1 instead of the last n lines",
				},
				condensed = {
					type = "boolean",
					description = "Collapse blank runs into span markers (default true); false returns the raw grid rows",
				},
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
			local start = args and args.start_line
			local condensed = not (args and args.condensed == false)
			local r = read_tail(t, name, { n = n, start = start and math.max(1, math.floor(start)) or nil })
			local out, truncated = present_lines(r, n, condensed)
			return {
				name = name,
				running = r.running,
				dead = r.dead,
				pid = r.pid,
				exit_code = r.exit_code,
				total_lines = r.total_lines,
				line_range = r.line_range,
				condensed = condensed,
				cursor = state_for(t, name) and term_cursor((state_for(t, name) or {}).buf) or nil,
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
				wait_ms = {
					type = "integer",
					description = "Wait up to this many ms for the terminal to commit output after sending, before reading (default 250). Output may land at the TOP of a fresh shell screen; the result reports where content was found.",
				},
				read_lines = {
					type = "integer",
					description = "Include up to this many lines in the tool result (default 25; 0 skips the read)",
				},
				condensed = {
					type = "boolean",
					description = "Collapse blank runs into span markers (default true); false returns the raw grid rows",
				},
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
			local wait_ms = args and args.wait_ms
			local read_lines_out = (args and args.read_lines)
			read_lines_out = (read_lines_out == nil and 25) or math.max(0, math.floor(read_lines_out))
			local ok, res = pcall(t.send, name, data, { lstrip = false, wait_ms = wait_ms })
			if not ok or type(res) ~= "table" then
				error("sending to terminal '" .. name .. "' failed: " .. tostring(res))
			end
			if res.ok then
				local condensed = not (args and args.condensed == false)
				local out = {
					sent = true,
					name = name,
					bytes = #data,
					settled = res.settled == 1 or res.settled == true or false,
				}
				if read_lines_out > 0 then
					local n = output_cap(read_lines_out)
					local okread, r = pcall(read_tail, t, name, { n = n })
					if okread and type(r) == "table" then
						local read_from = "end"
						if not has_content(r.lines) then
							-- fresh screens paint at the top; re-read from the head
							local okhead, h = pcall(read_tail, t, name, { n = n, start = 1 })
							if okhead and type(h) == "table" and h.line_range ~= nil and has_content(h.lines) then
								r = h
								read_from = "start"
							end
						end
						out.read_from = read_from
						out.total_lines = r.total_lines
						out.line_range = r.line_range
						out.running = r.running
						out.dead = r.dead
						out.pid = r.pid
						out.exit_code = r.exit_code
						out.condensed = condensed
						local buf = state_for(t, name) and state_for(t, name).buf
						out.cursor = term_cursor(buf)
						local out_lines, truncated = present_lines(r, n, condensed)
						out.lines = out_lines
						out.truncated = truncated
					else
						out.read_error = tostring(r)
					end
				end
				return out
			end
			-- Denial: surface the authorization reason verbatim as a tool error.
			error(
				("term_send denied (%s)%s"):format(res.reason or "unknown", res.detail and (": " .. tostring(res.detail)) or "")
			)
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
