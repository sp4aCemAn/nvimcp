local M = {}

local patterns = {
	-- Bare keys / tokens (Lua patterns: use + quantifier, not {n})
	"AKIA[0-9A-Z]+",      -- AWS access key id
	"AIza[0-9A-Za-z%-_]+", -- Google API key
	"gh[pousr]_%w+",      -- GitHub tokens (ghp_ gho_ ghu_ ghs_ ghr_)
	"github_pat_%w+",     -- GitHub fine-grained PAT
	"sk%-%w+",            -- OpenAI/Anthropic style secret keys
	"xox[baprs]%-%w+",    -- Slack tokens
	"[Bb]earer%s+[A-Za-z0-9%-%._~+/]+=*",
	string.rep("%x", 64) .. "+", -- long hex (64+)
}

local keyvals = {
	"api[_-]?key",
	"apikey",
	"secret",
	"token",
	"password",
	"passwd",
	"authorization",
}

for _, k in ipairs(keyvals) do
	table.insert(patterns, k .. "%s*[=:>%s]%s*%S+")
	table.insert(patterns, k:upper():gsub("%-", "%%-") .. "%s*[=:>%s]%s*%S+")
	table.insert(patterns, k:upper():sub(1, 1) .. k:sub(2) .. "%s*[=:>%s]%s*%S+")
end

function M.scrub(s)
	if type(s) ~= "string" then
		return s
	end
	local out = s
	for _, p in ipairs(patterns) do
		out = out:gsub(p, "[REDACTED]")
	end
	out = out:gsub(string.rep("[A-Za-z0-9+/]", 40) .. "=*", "[REDACTED]")
	return out
end

function M.scrub_lines(lines)
	local out = {}
	for i, l in ipairs(lines) do
		out[i] = M.scrub(l)
	end
	return out
end

-- Truncate an array-like table of items; never mixes notice strings into data --
-- instead return truncated as a separate flag so callers can report it.
function M.truncate_table(items, max)
	local total = #items
	local truncated = total > max
	local out = vim.list_slice(items, 1, math.max(0, max))
	return out, total, truncated
end

return M
