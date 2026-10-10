local root = vim.fn.getcwd()
vim.opt.rtp:prepend(root)
package.path = root .. "/lua/?.lua;" .. root .. "/lua/?/init.lua;" .. package.path

local nvimcp = require("nvimcp")
nvimcp.setup({ autostart = false })
assert(nvimcp.start(), "start failed")

local sock = require("nvimcp.server").socket_path()
local f = assert(io.open(root .. "/tests/.boot.sock", "w"))
f:write(sock)
f:close()
local g = assert(io.open(root .. "/tests/.boot.pid", "w"))
g:write(tostring(vim.fn.getpid()))
g:close()

local buf = vim.api.nvim_create_buf(true, false)
vim.api.nvim_buf_set_lines(buf, 0, -1, false, { "original line one", "original line two" })
vim.api.nvim_set_current_buf(buf)
vim.bo[buf].modified = false

while vim.fn.filereadable(root .. "/tests/.stop") == 0 do
	vim.wait(200)
end
vim.fn.delete(root .. "/tests/.stop")
vim.fn.delete(root .. "/tests/.boot.sock")
vim.fn.delete(root .. "/tests/.boot.pid")
