local config = require("nvimcp.config")
local M = {}

M.registry = {}
M.loaded = false

function M.register(tool)
	assert(tool and tool.name and tool.handler and tool.inputSchema, "invalid tool spec")
	M.registry[tool.name] = tool
end

function M.load_builtins()
	if M.loaded then
		return
	end
	M.loaded = true
	local buffer = require("nvimcp.tools.buffer")
	buffer.register_readonly()
	if config.options.allow_edit then
		buffer.register_edit()
	end
	require("nvimcp.tools.lsp").register()
	if config.options.terminals.enabled then
		local ok, term = pcall(require, "nvimcp.tools.term")
		if ok then
			term.register()
		else
			vim.notify("[nvimcp] terminals.enabled is set but nvimcp.tools.term is not available (phase 3)", vim.log.levels.WARN)
		end
	end
end

function M.disabled(name)
	return vim.list_contains(config.options.disable or {}, name)
end

function M.list()
	M.load_builtins()
	local out = {}
	for _, tool in pairs(M.registry) do
		if not M.disabled(tool.name) then
			table.insert(out, {
				name = tool.name,
				description = tool.description,
				inputSchema = tool.inputSchema,
			})
		end
	end
	table.sort(out, function(a, b)
		return a.name < b.name
	end)
	return out
end

function M.call(name, args)
	M.load_builtins()
	local tool = M.registry[name]
	if not tool or M.disabled(name) then
		return nil, "unknown tool: " .. tostring(name)
	end
	if args == nil then
		args = {}
	end
	if type(args) ~= "table" then
		return nil, "arguments must be an object"
	end
	local ok, res = pcall(tool.handler, args)
	if not ok then
		return nil, "tool error: " .. tostring(res)
	end
	return res, nil
end

return M
