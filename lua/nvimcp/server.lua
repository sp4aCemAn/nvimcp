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

function M.sessions_dir()
	return M.socket_dir() .. "/sessions"
end

-- Own registry shard: one file per nvim pid, {pid = entry} inside.
function M.sessions_path()
	return M.sessions_dir() .. "/" .. tostring(vim.fn.getpid()) .. ".json"
end

local function ensure_dir()
	local dir = M.socket_dir()
	if vim.fn.isdirectory(dir) == 0 then
		vim.fn.mkdir(dir, "p", "0700")
	end
	return dir
end

local function session_entry()
	return {
		pid = vim.fn.getpid(),
		socket = sock,
		cwd = vim.uv.cwd(),
		config = vim.fn.stdpath("config"),
		registered_at = os.time(),
		fallback_recent = config.options.fallback_recent and true or false,
	}
end

local function shard_write(path, data)
	local tmp = path .. string.format(".%d.tmp", vim.fn.getpid())
	local fh, err = vim.uv.fs_open(tmp, "w", 384)
	if not fh then
		vim.notify("[nvimcp] sessions write failed: " .. tostring(err), vim.log.levels.WARN)
		return
	end
	local _, werr = vim.uv.fs_write(fh, vim.json.encode(data) .. "\n")
	vim.uv.fs_close(fh)
	if werr then
		vim.notify("[nvimcp] sessions write failed: " .. tostring(werr), vim.log.levels.WARN)
		pcall(vim.uv.fs_unlink, tmp)
		return
	end
	local _, rerr = vim.uv.fs_rename(tmp, path)
	if rerr then
		vim.notify("[nvimcp] sessions write failed: " .. tostring(rerr), vim.log.levels.WARN)
		pcall(vim.uv.fs_unlink, tmp)
	end
end

-- Unreadable/undecodable shards count as stale; a live session's shard is
-- always complete (tmp + rename), so pruning can never see a torn file.
local function shard_stale(path)
	local okf, lines = pcall(vim.fn.readfile, path)
	if not okf or not lines or #lines == 0 then
		return true
	end
	local okd, data = pcall(vim.json.decode, table.concat(lines, "\n"))
	if not okd or type(data) ~= "table" then
		return true
	end
	for _, entry in pairs(data) do
		local entry_socket = type(entry) == "table" and entry.socket
		if entry_socket and vim.uv.fs_stat(entry_socket) ~= nil then
			return false
		end
	end
	return true
end

-- One shard per nvim pid: a writer only ever touches its own file, so
-- concurrent sessions cannot clobber each other's registry entry (the old
-- shared sessions.json read-modify-write raced and silently lost live
-- sessions). Shards keep the legacy {pid = entry} shape so readers parse
-- old files and shards interchangeably. Stale shards (dead socket) are
-- unlinked on update — unlink-only, no shared writes, race-free. Safe by
-- construction: M.start binds the socket before the first sessions_update,
-- and M.stop unlinks the socket before the shard, so a live session's shard
-- can only be pruned a moment before its own remove deletes it anyway.
local function sessions_update()
	if not config.options.write_sessions then
		return
	end
	local dir = M.sessions_dir()
	if vim.fn.isdirectory(dir) == 0 then
		vim.fn.mkdir(dir, "p", "0700")
	end
	for _, path in ipairs(vim.fn.glob(dir .. "/*.json", false, true)) do
		if path ~= M.sessions_path() and shard_stale(path) then
			pcall(vim.uv.fs_unlink, path)
		end
	end
	shard_write(M.sessions_path(), { [tostring(vim.fn.getpid())] = session_entry() })
end

local function sessions_remove()
	if not config.options.write_sessions then
		return
	end
	pcall(vim.uv.fs_unlink, M.sessions_path())
	-- tidy: best-effort; fails while other sessions still hold shards
	pcall(vim.uv.fs_rmdir, M.sessions_dir())
end

local function arm_exit_cleanup()
	vim.api.nvim_create_augroup("nvimcp", { clear = true })
	vim.api.nvim_create_autocmd("VimLeavePre", {
		group = "nvimcp",
		callback = function()
			M.stop()
		end,
	})
	-- Keep the session entry's cwd honest when the user moves the session;
	-- proxies match sessions by cwd, so a stale entry misdirects discovery.
	vim.api.nvim_create_autocmd("DirChanged", {
		group = "nvimcp",
		callback = function()
			pcall(sessions_update)
		end,
	})
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
	arm_exit_cleanup()
	pcall(sessions_update)
	return sock
end

function M.stop()
	if not sock then
		return true
	end
	pcall(vim.fn.serverstop, sock)
	pcall(sessions_remove)
	pcall(vim.api.nvim_del_augroup_by_name, "nvimcp")
	sock = nil
	call_times = {}
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
	-- Record only allowed calls: rejected attempts must not extend the window,
	-- otherwise a retrying client keeps itself locked out (livelock).
	if #kept >= rl.calls then
		call_times = kept
		return false, #kept
	end
	table.insert(kept, now)
	call_times = kept
	return true, #kept
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
	if not sock then
		return { error = "nvimcp not started in this session" }
	end
	local ok, allowed, count = pcall(rate_ok)
	if ok and not allowed then
		return { error = ("rate limit exceeded (%d calls in window)"):format(count) }
	end
	local ok2, res = pcall(handle, method, params)
	if not ok2 then
		return { error = "internal error: " .. tostring(res) }
	end
	return res
end

return M
