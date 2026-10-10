local sanitize = require("nvimcp.sanitize")
local tools = require("nvimcp.tools")

local M = {}

local REQ_TIMEOUT = 2000
local MAX_LOCATIONS = 100
local MAX_DIAGNOSTICS = 200

local function bufnr_for(bid)
	bid = bid or vim.api.nvim_get_current_buf()
	if not vim.api.nvim_buf_is_valid(bid) then
		error("buffer_id does not exist: " .. tostring(bid))
	end
	return bid
end

local function position_of(bid, line, character)
	if line and character then
		return { line = math.max(0, line - 1), character = math.max(0, character) }
	end
	local win = vim.fn.bufwinid(bid)
	if win ~= -1 then
		local cur = vim.api.nvim_win_get_cursor(win)
		return { line = cur[1] - 1, character = cur[2] }
	end
	return { line = 0, character = 0 }
end

local function as_array(v)
	if type(v) ~= "table" then
		return {}
	end
	if v[1] ~= nil then
		return v
	end
	return { v }
end

local function request_sync(bid, method, params)
	local res = vim.lsp.buf_request_sync(bid, method, params, REQ_TIMEOUT)
	if not res then
		return {}
	end
	local flat = {}
	for _, client_res in pairs(res) do
		if client_res and client_res.result then
			table.insert(flat, client_res.result)
		end
	end
	return flat
end

local function line_at(path, zero_line)
	local path_bufnr = vim.fn.bufnr(path)
	if path_bufnr ~= -1 and vim.api.nvim_buf_is_loaded(path_bufnr) then
		return vim.api.nvim_buf_get_lines(path_bufnr, zero_line, zero_line + 1, false)[1] or ""
	end
	local ok, lines = pcall(vim.fn.readfile, path, "", zero_line + 1)
	if ok and type(lines) == "table" and lines[zero_line + 1] then
		return lines[zero_line + 1]
	end
	return ""
end

local function location_item(item)
	local uri = item.uri or item.targetUri
	if not uri then
		return nil
	end
	local range = item.range or item.targetSelectionRange
	if not range then
		return nil
	end
	local path = vim.uri_to_fname(uri)
	return {
		path = path,
		line = range.start.line + 1,
		character = range.start.character,
		text = sanitize.scrub(line_at(path, range.start.line)),
	}
end

local function flatten_locations(result)
	local out = {}
	for _, r in ipairs(as_array(result)) do
		for _, item in ipairs(as_array(r)) do
			-- Location {uri, range} or LocationLink {targetUri, targetSelectionRange}
			local loc = location_item(item)
			if loc then
				table.insert(out, loc)
			end
		end
	end
	return out
end

local function content_text(contents)
	if type(contents) == "string" then
		return contents
	end
	if type(contents) == "table" then
		if contents.value then
			return contents.value
		end
		local parts = {}
		for _, c in ipairs(contents) do
			if type(c) == "string" then
				table.insert(parts, c)
			elseif type(c) == "table" and c.value then
				table.insert(parts, c.value)
			end
		end
		return table.concat(parts, "\n")
	end
	return vim.inspect(contents)
end

local function register_lsp_clients()
	tools.register({
		name = "lsp_clients",
		description = "List LSP clients attached to a buffer (or all active clients).",
		inputSchema = {
			type = "object",
			properties = {
				buffer_id = { type = "integer", description = "Buffer id, defaults to current" },
			},
		},
		handler = function(args)
			local opts = {}
			if args and args.buffer_id then
				opts.bufnr = bufnr_for(args.buffer_id)
			end
			local out = {}
			for _, c in ipairs(vim.lsp.get_clients(opts)) do
				local cfg = c.config or {}
				table.insert(out, {
					id = c.id,
					name = c.name,
					root_dir = cfg.root_dir,
					filetypes = cfg.filetypes,
					attached_buffers = vim.tbl_keys(c.attached_buffers or {}),
				})
			end
			return { clients = out, count = #out }
		end,
	})
end

local function register_lsp_diagnostics()
	tools.register({
		name = "lsp_diagnostics",
		description = "Get diagnostics (from LSP and other sources) for a buffer (or all buffers) via vim.diagnostic. severity_max: 1=errors only, 2=errors+warnings, 3=+info, 4=+hints (default).",
		inputSchema = {
			type = "object",
			properties = {
				buffer_id = { type = "integer", description = "Buffer id, defaults to all buffers" },
				severity_max = {
					type = "integer",
					description = "Include diagnostics down to this severity (1 error, 2 warning, 3 info, 4 hint; default 4)",
				},
			},
		},
		handler = function(args)
			local opts = {}
			if args.severity_max then
				-- nvim semantics are inverted: {min=X} keeps severities numerically <= X
				opts.severity = { min = args.severity_max }
			end
			local bufs = args.buffer_id and { bufnr_for(args.buffer_id) } or vim.api.nvim_list_bufs()
			local out = {}
			for _, bid in ipairs(bufs) do
				if bid == 0 or vim.api.nvim_buf_is_valid(bid) then
					for _, d in ipairs(vim.diagnostic.get(bid, opts)) do
						table.insert(out, {
							buffer_id = d.bufnr,
							path = vim.api.nvim_buf_get_name(d.bufnr),
							line = d.lnum + 1,
							col = d.col + 1,
							severity = vim.diagnostic.severity[d.severity] or d.severity,
							message = sanitize.scrub(d.message),
							source = d.source,
							code = d.code,
						})
					end
				end
			end
			table.sort(out, function(a, b)
				if a.path ~= b.path then
					return a.path < b.path
				end
				if a.line ~= b.line then
					return a.line < b.line
				end
				return (a.col or 0) < (b.col or 0)
			end)
			local total = #out
			local truncated = total > MAX_DIAGNOSTICS
			if truncated then
				out = vim.list_slice(out, 1, MAX_DIAGNOSTICS)
			end
			return { diagnostics = out, count = total, truncated = truncated }
		end,
	})
end

local function register_lsp_definition()
	tools.register({
		name = "lsp_definition",
		description = "Go to definition of the symbol under a buffer position, via the active LSP.",
		inputSchema = {
			type = "object",
			properties = {
				buffer_id = { type = "integer", description = "Buffer id, defaults to current" },
				line = { type = "integer", description = "1-based line, defaults to cursor line" },
				character = { type = "integer", description = "0-based column, defaults to cursor column" },
			},
		},
		handler = function(args)
			local bid = bufnr_for(args.buffer_id)
			local params = {
				textDocument = { uri = vim.uri_from_bufnr(bid) },
				position = position_of(bid, args.line, args.character),
			}
			local out = {}
			for _, result in ipairs(request_sync(bid, "textDocument/definition", params)) do
				vim.list_extend(out, flatten_locations(result))
				if #out >= MAX_LOCATIONS then
					break
				end
			end
			local truncated = #out > MAX_LOCATIONS
			if truncated then
				out = vim.list_slice(out, 1, MAX_LOCATIONS)
			end
			return { definitions = out, count = #out, truncated = truncated }
		end,
	})
end

local function register_lsp_references()
	tools.register({
		name = "lsp_references",
		description = "List references to the symbol under a buffer position, via the active LSP.",
		inputSchema = {
			type = "object",
			properties = {
				buffer_id = { type = "integer", description = "Buffer id, defaults to current" },
				line = { type = "integer", description = "1-based line, defaults to cursor line" },
				character = { type = "integer", description = "0-based column, defaults to cursor column" },
				include_declaration = { type = "boolean", description = "Include the declaration itself (default false)" },
			},
		},
		handler = function(args)
			local bid = bufnr_for(args.buffer_id)
			local params = {
				textDocument = { uri = vim.uri_from_bufnr(bid) },
				position = position_of(bid, args.line, args.character),
				context = { includeDeclaration = (args.include_declaration or false) },
			}
			local out = {}
			for _, result in ipairs(request_sync(bid, "textDocument/references", params)) do
				vim.list_extend(out, flatten_locations(result))
				if #out >= MAX_LOCATIONS then
					break
				end
			end
			local truncated = #out > MAX_LOCATIONS
			if truncated then
				out = vim.list_slice(out, 1, MAX_LOCATIONS)
			end
			return { references = out, count = #out, truncated = truncated }
		end,
	})
end

local function register_lsp_hover()
	tools.register({
		name = "lsp_hover",
		description = "Hover info (docs/type signature) for the symbol under a buffer position, via the active LSP.",
		inputSchema = {
			type = "object",
			properties = {
				buffer_id = { type = "integer", description = "Buffer id, defaults to current" },
				line = { type = "integer", description = "1-based line, defaults to cursor line" },
				character = { type = "integer", description = "0-based column, defaults to cursor column" },
			},
		},
		handler = function(args)
			local bid = bufnr_for(args.buffer_id)
			local params = {
				textDocument = { uri = vim.uri_from_bufnr(bid) },
				position = position_of(bid, args.line, args.character),
			}
			local hovers = {}
			for _, result in ipairs(request_sync(bid, "textDocument/hover", params)) do
				if result and result.contents then
					table.insert(hovers, sanitize.scrub(content_text(result.contents)))
				end
			end
			return { hover = table.concat(hovers, "\n---\n"), found = #hovers > 0 }
		end,
	})
end

function M.register()
	register_lsp_clients()
	register_lsp_diagnostics()
	register_lsp_definition()
	register_lsp_references()
	register_lsp_hover()
end

return M
