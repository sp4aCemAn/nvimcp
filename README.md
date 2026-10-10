# nvimcp

MCP (Model Context Protocol) server for Neovim, shipped as a Neovim plugin.
Exposes your live nvim session — buffers and LSP state — to any MCP client
(opencode, Claude Code, Claude Desktop, Cursor, ...).

Design: all tool logic lives inside the nvim session as a Lua plugin; a thin
zero-dependency stdio proxy bridges any MCP client to it over Neovim's own
RPC socket. No `eval`-style escape hatch — clients call named, fixed tools
with JSON arguments only.

## Status

**Phase 1 + 2 (this release):** in-nvim server + buffer/LSP tools, plus the
zero-dependency stdio proxy that lets any MCP client (opencode, Claude Code,
Claude Desktop, Cursor, ...) drive a live nvim session. Phase 3 adds terminal
integration (via [nvimterminals](https://github.com/sp4aCemAn/nvimterminals))
with in-nvim command approval.

## Requirements

- Neovim 0.12+
- Python 3.8+ (only for the stdio proxy; the nvim plugin itself needs nothing else)

## Install

With lazy.nvim:

```lua
{
	"sp4aCemAn/nvimcp",
	config = function()
		require("nvimcp").setup({})
	end,
}
```

By default the server starts automatically (`autostart = true`).

## Commands

- `:NvimcpStart` — start the server
- `:NvimcpStop` — stop it
- `:NvimcpStatus` — floating status pane (socket path, session file, proxy path, tool list)

## Connecting MCP clients (phase 2)

	MCP client <— stdio (line-delimited JSON-RPC) —> nvimcp-proxy
	          <— unix socket (msgpack-RPC) —> live nvim —> nvimcp tools

`proxy/nvimcp-proxy.py` (Python stdlib only) finds your nvim session
automatically: `--socket` wins; else `--pid` picks from the sessions file; else
**terminal ancestry** — an agent launched inside one of this session's
terminals (any process descendant of that nvim) belongs to that session, even
if the agent's shell cd'ed to a different directory; else the session whose
nvim cwd matches the directory the client was launched
from, or the closest ancestor/descendant of it (nvim at the repo root, client
in a subdir); else, with several live sessions left, it connects only to a
session whose nvimcp config set `fallback_recent = true` (warned; strict
refusal otherwise), or to the sole live session when that's the only one.
Entries left behind by crashed
nvim sessions are detected by pid and ignored. Sessions are discovered from
`$XDG_RUNTIME_DIR/nvimcp/sessions.json`; on macOS (where that variable is
unset) the proxy scans `<TMPDIR>/nvim.*/<random>/nvimcp/sessions.json`
instead, since nvim's per-process runtime directory is not a stable path
there. It connects lazily on the first tool
request, so nvim can be started before or after the client, and it reconnects
if you restart nvim mid-session.

Your client config needs the proxy's absolute path — where that lives depends
on your plugin manager, so don't guess: run `:NvimcpStatus` in nvim and copy
the `proxy:` line (or `:lua print(require("nvimcp").proxy_path())`). Client
configs don't expand `~` or env vars, so paste the real absolute path. With
lazy.nvim it is typically
`/home/you/.local/share/nvim/lazy/nvimcp/proxy/nvimcp-proxy.py`
(macOS: `/Users/you/.local/share/nvim/lazy/nvimcp/proxy/nvimcp-proxy.py`).

opencode — `opencode.json`:

```json
{
	"mcp": {
		"nvimcp": {
			"type": "local",
			"command": ["python3", "/home/you/.local/share/nvim/lazy/nvimcp/proxy/nvimcp-proxy.py"],
			"enabled": true
		}
	}
}
```

Claude Code:

```sh
claude mcp add nvimcp -- python3 /home/you/.local/share/nvim/lazy/nvimcp/proxy/nvimcp-proxy.py
```

Claude Desktop (`claude_desktop_config.json`) and Cursor (`~/.cursor/mcp.json`):

```json
{
	"mcpServers": {
		"nvimcp": {
			"command": "python3",
			"args": ["/home/you/.local/share/nvim/lazy/nvimcp/proxy/nvimcp-proxy.py"]
		}
	}
}
```

Multiple nvim sessions? Pin one with `--pid <pid>` (see `:NvimcpStatus` or the
sessions file) or `--socket <path>`. Agents can be chatty — consider
`rate_limit = { calls = 60, window = 5 }` when pairing with an MCP client.
Debugging: run the proxy directly with `--debug` and paste JSON lines into it.

## Config

```lua
require("nvimcp").setup({})
-- defaults:
{
	autostart = true,            -- start server on load
	start_if = nil,              -- function():boolean to suppress autostart
	socket_dir = nil,            -- default: $XDG_RUNTIME_DIR-equivalent (stdpath("run"))/nvimcp
	write_sessions = true,       -- maintain sessions.json for client discovery
	rate_limit = { calls = 10, window = 5 }, -- tool calls per window (seconds); nil disables
	max_output_lines = 500,      -- cap on lines returned per tool call
	allow_edit = true,           -- register buffer_edit; false = tool not registered (read-only server)
	disable = {},                -- tool names to unregister, e.g. { "buffer_edit" }
	terminals = {                -- reserved for phase 3
		enabled = false,
		approve = true,
	},
}
```

Proof-vault mode (read-only, for untrusted codebases):

```lua
{ allow_edit = false }
```

## Tools

Buffers:

- `buffer_list` — loaded buffers (id, path, filetype, modified, buftype)
- `buffer_info` — detailed info for a buffer incl. cursor and diagnostics counts
- `buffer_read` — read lines (ranged); output always scrubbed + truncated
- `buffer_edit` — replace a line range; refuses modified or readonly buffers
  unless `force=true`, never touches non-modifiable buffers; `end_line` may
  exceed the line count to append
- `buffer_search` — literal string search (or Lua pattern with `regex=true`);
  caps at 200 matches, `truncated=true` means there may be more

LSP:

- `lsp_clients` — clients attached to a buffer
- `lsp_diagnostics` — diagnostics for a buffer or all buffers (sorted by
  path/line); `severity_max` narrows results (1=errors only, 2=+warnings,
  3=+info, 4=+hints default)
- `lsp_definition` / `lsp_references` / `lsp_hover` — position-based LSP requests
  (default to the cursor position of the target buffer, so editor state can
  steer the AI's queries)

Terminals (opt-in, requires the
[nvimterminals](https://github.com/sp4aCemAn/nvimterminals) plugin):

- `term_list` — named terminals (name, running/dead, pid, exit code, agent
  permission, line count); dead terminals keep their scrollback
- `term_read` — last n lines of a terminal's scrollback (scrubbed + capped like
  `buffer_read`); read-only, always available
- `term_send` — send a line of input to a terminal; **authorization lives
  entirely in nvimterminals**: the terminal needs agent control enabled
  (`:TermAgent`) and its command policy (`:TermPolicy`) must allow the line
  (deny rules win, `*` allows all, empty policy denies everything). Denials
  come back as tool errors carrying the reason string verbatim. The tools only
  register when `terminals.enabled = true` and nvimterminals is loaded;
  `term_send` additionally requires nvimterminals with the agent-control API.

## Security

- Communication happens over a user-owned Unix socket at
  `stdpath("run")/nvimcp/nvimcp-<pid>.sock` (mode 600) — on Linux
  that resolves to `$XDG_RUNTIME_DIR/nvimcp/...`, on macOS to
  `<TMPDIR>/nvim.<user>/<random>/nvimcp/...`. No TCP listening.
- Tool handlers are fixed Lua functions; the client sends arguments, never code.
- The proxy is a fixed translator: it calls one hard-coded `nvim_exec_lua`
  expression (`require('nvimcp.server').rpc`); client data only ever crosses
  as tool arguments, never as code.
- All tool output is scrubbed unconditionally (AWS/GCP/GitHub/OpenAI/Slack
  keys, bearer tokens, `password=`-style assignments, long hex/base64 blobs)
  and capped by `max_output_lines`; there is no config toggle to disable it.
- `allow_edit` is read when the tool registry first loads; changing it in a
  live session only affects new registrations (a running server that already
  registered `buffer_edit` still refuses calls while `allow_edit=false`).
- ✱Terminal tools (phase 3) are read-only by default; `term_send` is authorized
  exclusively by nvimterminals (per-terminal agent toggle + command policy,
  denials never run the command). nvimcp never overrides that policy.

## Running the tests

```sh
nvim --headless -l tests/lifecycle.lua   # unit + tool behavior (exit 0 = pass)
./tests/integration.sh                   # cross-process round-trip over the socket
./tests/proxy.sh                         # phase 2: msgpack unit + MCP-over-stdio e2e (needs python3)
nvim --headless -l tests/terminal.lua    # phase 3: term_* tools e2e (needs nvimterminals on
                                         # the runtimepath or NVIMTERMINALS_PATH set)
```
