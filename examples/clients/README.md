# MCP client configurations

One config snippet per MCP client. All point at the quickstart profile
(`examples/quickstart/profile.yml`) so you can clone the repo, drop the
snippet into the right place, and have rocannon's typed tools show up in
your client immediately.

Replace the absolute repo path (`/path/to/rocannon`) with wherever you
cloned the repo.

| Client | File location | Notes |
|---|---|---|
| **Claude Code** (the CLI) | `.mcp.json` in the project root, or `claude mcp add ...` | The repo ships a working `.mcp.json` at root that picks up the quickstart profile when you run `claude` from the repo. |
| **Claude Desktop** | macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`<br>Windows: `%APPDATA%\Claude\claude_desktop_config.json` | See `claude-desktop.json`. |
| **Cursor** | `.cursor/mcp.json` (per-project) or `~/.cursor/mcp.json` (global) | See `cursor.json`. Same shape as Claude. |
| **mcphost** | `~/.mcphost.yml` (default) or `--config <path>` | See `mcphost.json`. JSON is fine; the YAML form in mcphost's own docs is also supported. |
| **IBM Bob** | `.bob/mcp.json` (project) or `~/.bob/mcp_settings.json` (global) | See `ibm-bob.json`. Supports the same envelope plus optional `cwd`, `alwaysAllow`, `disabled`, `timeout`. |

## Quick start with Claude Code

The fastest path: from a checkout of this repo, run

```bash
claude mcp add rocannon --scope local -- \
  uv run --directory "$(pwd)" rocannon mcp serve \
  --profile "$(pwd)/examples/quickstart/profile.yml"
claude mcp get rocannon          # health check
claude                            # tools/refresh to see them
```

To verify Claude Code can spawn rocannon outside of an interactive session:

```bash
claude mcp get rocannon
# Expected: "Status: ✓ Connected"
```

## What gets registered

All of these point at `profile.yml` from `examples/quickstart/`, which loads
two cannons:

- Ansible (`ansible.builtin.ping`, `command`, `setup`, `debug`)
- Terraform (null + random providers, all resources)

Plus the cross-cannon meta tools: `save_playbook`, `commit_session`.
Total: 19 tools, 2 resources, 2 resource templates.
