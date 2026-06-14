# examples/

Sample profiles, inventories, and client configs.

| Path | What it is |
|---|---|
| [`quickstart/`](quickstart/) | Profile + inventory targeting localhost via `ansible_connection=local`. Loads `ansible.builtin.{ping,command,setup,debug}`. |
| [`case-study/`](case-study/) | Claude Haiku (via the Claude Agent SDK) driving Rocannon's MCP tools from natural language against a real RHEL 9 node: facts, an ad-hoc command, a config change, plus reflection and replay as standard `ansible-playbook`. `agent_demo.py` + `run.sh` reproduce it. |
| [`execution-environment/`](execution-environment/) | Rocannon baked into an Ansible Execution Environment (`ansible-builder`), giving a frozen, deterministic MCP tool surface, driven from natural language by Haiku. |
| [`containerlab/`](containerlab/) | Claude Haiku driving Rocannon against a two-node Arista cEOS fabric under [containerlab](https://containerlab.dev): the `arista.eos` modules become MCP tools (facts, `show` commands over eAPI, a banner config change). `agent_demo.py` reproduces it. |
| [`profiles/`](profiles/) | Topical profiles: one per common scenario (Docker, Postgres, MySQL, MongoDB, RabbitMQ, crypto, sysadmin, local-dev). Point `rocannon mcp serve --profile <one>` at any of them. |
| [`inventories/`](inventories/) | Inventories the topical profiles reference: `local.yml` (localhost via local connection) and `podman.yml` (SSH into LinuxONE test containers on ports 2222-2224). |
| [`clients/`](clients/) | One MCP-client config per file: Claude Desktop, Cursor, mcphost, IBM Bob (Shell + IDE). Drop into the client's expected path. See `clients/README.md` for which path. |

The repo-root [`.mcp.json`](../.mcp.json) is the Claude Code project config and points at `quickstart/profile.yml` so `claude` auto-discovers Rocannon from a fresh checkout.
