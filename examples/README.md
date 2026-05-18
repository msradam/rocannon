# examples/

Sample profiles, inventories, and client configs.

| Path | What it is |
|---|---|
| [`quickstart/`](quickstart/) | Profile + inventory targeting localhost via `ansible_connection=local`. Loads `ansible.builtin.{ping,command,setup,debug}`. |
| [`profiles/`](profiles/) | Topical profiles: one per common scenario (Docker, Postgres, MySQL, MongoDB, RabbitMQ, crypto, sysadmin, local-dev). Point `rocannon mcp serve --profile <one>` at any of them. |
| [`inventories/`](inventories/) | Inventories the topical profiles reference: `local.yml` (localhost via local connection) and `podman.yml` (SSH into LinuxONE test containers on ports 2222-2224). |
| [`clients/`](clients/) | One MCP-client config per file: Claude Desktop, Cursor, mcphost, IBM Bob (Shell + IDE). Drop into the client's expected path. See `clients/README.md` for which path. |

The repo-root [`.mcp.json`](../.mcp.json) is the Claude Code project config and points at `quickstart/profile.yml` so `claude` auto-discovers rocannon from a fresh checkout.
