local sanitize = require("nvimcp.sanitize")
local config = require("nvimcp.config")
local tools = require("nvimcp.tools")

local M = {}

local function bufnr_for(bid)
	bid = bid or vim.api.nvim_get_current_buf()
	if not vim.api.nvim_buf_is_valid(bid) then
		error("buffer_id does not exist: " .. tostring(bid))
	end
	return bid
end

local function max_lines()
	return config.options.max_output_lines or 500
end

local function buf_summary(bid)
	local buftype = vim.bo[bid].buftype
	local summary = {
		id = bid,
		name = vim.api.nvim_buf_get_name(bid),
		buftype = buftype,
		filetype = vim.bo[bid].filetype,
		modified = vim.bo[bid].modified,
		line_count = vim.api.nvim_buf_line_count(bid),
		is_current = bid == vim.api.nvim_get_current_buf(),
	}
	if buftype == "terminal" then
		summary.terminal = true
	end
	return summary
end

local function register_buffer_list()
	tools.register({
		name = "buffer_list",
		description = "List all loaded buffers with basic info (id, path, filetype, modified state, buftype). Terminal buffers are flagged with terminal=true.",
		inputSchema = {
			type = "object",
			properties = {
				include_unlisted = {
					type = "boolean",
					description = "Include unlisted buffers (default false)",
				},
			},
		},
		handler = function(args)
			local out = {}
			for _, bid in ipairs(vim.api.nvim_list_bufs()) do
				if vim.bo[bid].buflisted or (args and args.include_unlisted) then
					table.insert(out, buf_summary(bid))
				end
			end
			return { buffers = out, count = #out }
		end,
	})
end

local function register_buffer_info()
	tools.register({
		name = "buffer_info",
		description = "Detailed info about one buffer (current buffer by default): cursor position, window, filetype, diagnostics summary.",
		inputSchema = {
			type = "object",
			properties = {
				buffer_id = { type = "integer", description = "Buffer id, defaults to current" },
			},
		},
		handler = function(args)
			local bid = bufnr_for(args and args.buffer_id)
			local info = buf_summary(bid)
			info.readonly = vim.bo[bid].readonly
			info.fixendofline = vim.bo[bid].fixendofline
			local win = vim.fn.bufwinid(bid)
			if win ~= -1 then
				local cursor = vim.api.nvim_win_get_cursor(win)
				info.cursor = { line = cursor[1], col = cursor[2] }
			end
			local diags = vim.diagnostic.get(bid)
			local counts = { error = 0, warn = 0, info = 0, hint = 0 }
			for _, d in ipairs(diags) do
				local sev = vim.diagnostic.severity[d.severity] or tostring(d.severity)
				if counts[sev] then
					counts[sev] = counts[sev] + 1
				end
			end
			info.diagnostics = counts
			info.hlsearch = vim.v.hlsearch == 1
			return info
		end,
	})
end

local function register_buffer_read()
	tools.register({
		name = "buffer_read",
		description = "Read lines from a buffer (current by default). Output is always scrubbed for secrets and capped by the server's max_output_lines setting.",
		inputSchema = {
			type = "object",
			properties = {
				buffer_id = { type = "integer", description = "Buffer id, defaults to current" },
				start_line = { type = "integer", description = "1-based start line, defaults to 1" },
				end_line = { type = "integer", description = "1-based end line, defaults to last line" },
				max_lines = { type = "integer", description = "Max lines returned (server caps at max_output_lines)" },
			},
		},
		handler = function(args)
			local bid = bufnr_for(args and args.buffer_id)
			local total = vim.api.nvim_buf_line_count(bid)
			local start = math.max(1, args.start_line or 1)
			local stop = math.min(total, args.end_line or total)
			local cap = math.min(args.max_lines or max_lines(), max_lines())
			local lines = vim.api.nvim_buf_get_lines(bid, start - 1, stop, false)
			lines = sanitize.scrub_lines(lines)
			local out, _, truncated = sanitize.truncate_table(lines, cap)
			return {
				buffer_id = bid,
				name = vim.api.nvim_buf_get_name(bid),
				total_lines = total,
				line_range = { start = start, stop = stop },
				lines = out,
				truncated = truncated,
			}
		end,
	})
end

function M.register_edit()
	tools.register({
		name = "buffer_edit",
		description = "Replace a range of lines in a buffer. Refuses modified buffers unless force=true, and refused outright for readonly buffers unless force=true (non-modifiable buffers are never editable). Text is a string split on newlines. end_line may exceed the current line count to append.",
		inputSchema = {
			type = "object",
			properties = {
				buffer_id = { type = "integer", description = "Buffer id, defaults to current" },
				start_line = { type = "integer", description = "1-based first line to replace" },
				end_line = { type = "integer", description = "1-based last line to replace (inclusive); may exceed line count to append" },
				text = { type = "string", description = "Replacement text; empty string deletes the range" },
				force = { type = "boolean", description = "Allow editing a modified or readonly buffer (default false)" },
			},
			required = { "start_line", "end_line", "text" },
		},
		handler = function(args)
			if not config.options.allow_edit then
				error("buffer_edit disabled by nvimcp config (allow_edit=false)")
			end
			local bid = bufnr_for(args.buffer_id)
			if not vim.bo[bid].modifiable then
				error("buffer is not modifiable")
			end
			if vim.bo[bid].modified and not (args.force or false) then
				error("buffer has unsaved changes; pass force=true only if that is intended")
			end
			if vim.bo[bid].readonly and not (args.force or false) then
				error("buffer is readonly; pass force=true only if that is intended")
			end
			local total = vim.api.nvim_buf_line_count(bid)
			local start = math.max(0, args.start_line - 1)
			local stop = math.max(0, math.min(args.end_line, total))
			local lines = {}
			if args.text ~= "" then
				lines = vim.split(args.text, "\n", { plain = true })
			end
			vim.api.nvim_buf_set_lines(bid, start, stop, false, lines)
			return {
				buffer_id = bid,
				replaced = { start = start + 1, stop = stop },
				line_count = vim.api.nvim_buf_line_count(bid),
			}
		end,
	})
end

local function register_buffer_search()
	tools.register({
		name = "buffer_search",
		description = "Search lines in a buffer. By default matches a literal string; set regex=true to use a Lua pattern (not regex).",
		inputSchema = {
			type = "object",
			properties = {
				buffer_id = { type = "integer", description = "Buffer id, defaults to current" },
				query = { type = "string", description = "String to look for" },
				regex = { type = "boolean", description = "Treat query as a Lua pattern" },
			},
			required = { "query" },
		},
		handler = function(args)
			local bid = bufnr_for(args.buffer_id)
			local needle = args.query
			if not needle or needle == "" then
				error("query is empty")
			end
			local literal = not (args.regex or false)
			local matches = {}
			for i, line in ipairs(vim.api.nvim_buf_get_lines(bid, 0, -1, false)) do
				local ok, from = pcall(string.find, line, needle, 1, literal)
				if ok and from then
					table.insert(matches, {
						line_number = i,
						text = sanitize.scrub(line),
					})
					if #matches >= 200 then
						break
					end
				end
			end
			return { buffer_id = bid, matches = matches, count = #matches }
		end,
	})
end

function M.register_readonly()
	register_buffer_list()
	register_buffer_info()
	register_buffer_read()
	register_buffer_search()
end

return M
