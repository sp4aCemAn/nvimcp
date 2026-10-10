local config = require("nvimcp.config")
local M = {}

local sock = nil
local call_times = {}

function M.socket_dir()
	return config.options.socket_dir or (vim.fn.stdpath("run") .. "/nvimcp")
end

function M.socket_path()
	return sock
end

function M.sessions_path()
	return M.socket_dir() .. "/sessions.json"
end

local function ensure_dir()
	local dir = M.socket_dir()
	if vim.fn.isdirectory(dir) == 0 then
		vim.fn.mkdir(dir, "p", "0700")
	end
	return dir
end

local function sessions_read()
	local f = M.sessions_path()
	local ok, lines = pcall(vim.fn.readfile, f)
	if not ok or not lines or #lines == 0 then
		return {}
	end
	local ok2, data = pcall(vim.json.decode, table.concat(lines, "\n"))
	if not ok2 or type(data) ~= "table" then
		return {}
	end
	return data
end

local function sessions_write(data)
	local f = M.sessions_path()
	vim.fn.writefile({ vim.json.encode(data) }, f)
	vim.uv.fs_chmod(f, 384)
end

local function sessions_update()
	if not config.options.write_sessions then
		return
	end
	local data = sessions_read()
	data[tostring(vim.fn.getpid())] = {
		pid = vim.fn.getpid(),
		socket = sock,
		cwd = vim.uv.cwd(),
		config = vim.fn.stdpath("config"),
	}
	sessions_write(data)
end

local function sessions_remove()
	if not config.options.write_sessions then
		return
	end
	local data = sessions_read()
	data[tostring(vim.fn.getpid())] = nil
	if vim.tbl_isempty(data) then
		pcall(vim.fn.delete, M.sessions_path())
	else
		sessions_write(data)
	end
end

function M.start()
	local dir = ensure_dir()
	local path = dir .. string.format("/nvimcp-%d.sock", vim.fn.getpid())
	if vim.uv.fs_stat(path) then
		vim.uv.fs_unlink(path)
	end
	local ok, addr = pcall(vim.fn.serverstart, path)
	if not ok or not addr then
		vim.notify("[nvimcp] serverstart failed: " .. tostring(addr), vim.log.levels.ERROR)
		return nil
	end
	sock = addr
	vim.uv.fs_chmod(sock, 384)
	sessions_update()
	return sock
end

function M.stop()
	if not sock then
		return true
	end
	pcall(vim.fn.serverstop, sock)
	sessions_remove()
	sock = nil
	return true
end

local function rate_ok()
	local rl = config.options.rate_limit
	if not rl or not rl.calls or not rl.window then
		return true
	end
	local now = vim.uv.now()
	local kept = {}
	for _, t in ipairs(call_times) do
		if now - t < rl.window * 1000 then
			table.insert(kept, t)
		end
	end
	table.insert(kept, now)
	call_times = kept
	return #kept <= rl.calls, #kept
end

local function handle(method, params)
	local tools = require("nvimcp.tools")
	if method == "list_tools" then
		return { tools = tools.list() }
	elseif method == "call_tool" then
		local name = params and params.name
		if not name then
			return { error = "missing tool name" }
		end
		local result, err = tools.call(name, (params or {}).arguments)
		if err then
			return { error = err }
		end
		return { result = result }
	end
	return { error = "unknown method: " .. tostring(method) }
end

function M.rpc(method, params)
	local ok, allowed, count = pcall(rate_ok)
	if ok and not allowed then
		return { error = ("rate limit exceeded (%d calls in window)"):format(count) }
	end
	if not sock then
		return { error = "nvimcp not started in this session" }
	end
	local ok2, res = pcall(handle, method, params)
	if not ok2 then
		return { error = "internal error: " .. tostring(res) }
	end
	return res
end

return M
