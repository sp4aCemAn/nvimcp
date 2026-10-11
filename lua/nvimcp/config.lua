local M = {}

local defaults = {
	autostart = true,
	start_if = nil,
	socket_dir = nil,
	write_sessions = true,
	fallback_recent = false, -- allow agents opened anywhere to reach THIS session when no cwd/ancestor matches (see README)
	rate_limit = { calls = 60, window = 5 },
	max_output_lines = 500,
	allow_edit = true,
	disable = {},
	terminals = {
		enabled = false,
		approve = true,
	},
}

-- Initialize with defaults so commands/tools work even before setup() runs
-- (e.g. :NvimcpStatus launched before lazy runs config).
M.options = vim.deepcopy(defaults)

function M.setup(opts)
	M.options = vim.tbl_deep_extend("force", vim.deepcopy(defaults), opts or {})
	return M.options
end

function M.get(key)
	return M.options[key]
end

return M
