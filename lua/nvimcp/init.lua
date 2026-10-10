local config = require("nvimcp.config")
local M = {}

M.started = false

local function version_ok()
	return vim.fn.has("nvim-0.12") == 1
end

function M.is_running()
	return M.started
end

function M.setup(opts)
	if not version_ok() then
		vim.notify("[nvimcp] requires Neovim 0.12+", vim.log.levels.ERROR)
		return
	end
	config.setup(opts)
	local cfg = config.options
	if cfg.autostart and (not cfg.start_if or cfg.start_if()) then
		M.start()
	end
end

function M.start()
	if not version_ok() then
		vim.notify("[nvimcp] requires Neovim 0.12+", vim.log.levels.ERROR)
		return false
	end
	if M.is_running() then
		return true
	end
	local server = require("nvimcp.server")
	local path = server.start()
	if path then
		M.started = true
		vim.notify("[nvimcp] listening on " .. path, vim.log.levels.DEBUG)
		return true
	end
	vim.notify("[nvimcp] failed to start", vim.log.levels.ERROR)
	return false
end

function M.stop()
	if not M.is_running() then
		return true
	end
	require("nvimcp.server").stop()
	M.started = false
	return true
end

function M.status()
	local server = require("nvimcp.server")
	local lines = {
		"nvimcp (Neovim MCP), nvim " .. vim.version().major .. "." .. vim.version().minor .. "." .. vim.version().patch,
		"running: " .. tostring(M.is_running()),
	}
	if M.is_running() then
		table.insert(lines, "socket: " .. server.socket_path())
		table.insert(lines, "sessions file: " .. server.sessions_path())
	else
		table.insert(lines, "socket: (not started)")
	end
	local names = vim.tbl_map(function(t)
		return t.name
	end, require("nvimcp.tools").list())
	table.insert(lines, "tools: " .. table.concat(names, ", "))
	return lines
end

return M
