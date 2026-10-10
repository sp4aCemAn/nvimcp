local function notify_err(subject, err)
	vim.notify(("[nvimcp] %s failed: %s"):format(subject, tostring(err)), vim.log.levels.ERROR)
end

vim.api.nvim_create_user_command("NvimcpStart", function()
	local ok, err = pcall(require("nvimcp").start)
	if not ok then
		notify_err("NvimcpStart", err)
	end
end, { desc = "Start the nvimcp MCP server" })

vim.api.nvim_create_user_command("NvimcpStop", function()
	local ok, err = pcall(require("nvimcp").stop)
	if not ok then
		notify_err("NvimcpStop", err)
	end
end, { desc = "Stop the nvimcp MCP server" })

vim.api.nvim_create_user_command("NvimcpStatus", function()
	local ok, lines = pcall(require("nvimcp").status)
	if not ok then
		notify_err("NvimcpStatus", lines)
		return
	end
	local buf = vim.api.nvim_create_buf(false, true)
	local width = 0
	for _, l in ipairs(lines) do
		width = math.max(width, #l)
	end
	vim.bo[buf].bufhidden = "wipe"
	vim.api.nvim_open_win(buf, true, {
		relative = "editor",
		width = width + 2,
		height = #lines + 2,
		col = math.floor((vim.o.columns - width) / 2),
		row = math.floor((vim.o.lines - #lines) / 2),
		style = "minimal",
		border = "rounded",
	})
	vim.api.nvim_buf_set_lines(buf, 0, -1, false, { "" })
	vim.api.nvim_buf_set_lines(buf, 1, -1, false, lines)
	vim.api.nvim_buf_set_lines(buf, -1, -1, false, { "" })
	vim.keymap.set("n", "q", function()
		vim.api.nvim_win_close(0, true)
	end, { desc = "Close nvimcp status", buffer = buf, silent = true })
end, { desc = "Show nvimcp status" })
