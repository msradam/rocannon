# Rocannon

<p align="center">
  <img src="docs/assets/gryphon.svg" alt="" width="120">
</p>

Rocannon is an MCP server that registers every installed Ansible module as a
typed tool. It reads `ansible-doc -j` for each module at startup and builds a
Pydantic-validated function signature, then exposes the result over the MCP
protocol (stdio or HTTP). Any MCP client (Claude Code, Cursor, mcphost,
custom agents) calls the same tools an operator would call from a REPL.

Sessions can be saved as Ansible playbooks under `.rocannon/playbooks/`. The
saved file is a real list-of-plays YAML that `ansible-playbook -i <inv> <file>`
runs directly. Rocannon also loads it back on next startup as an MCP prompt.

![demo](docs/assets/demo.gif)

## Install

```bash
pip install 'rocannon[ansible]'
```

`ansible-doc` and `ansible-runner` must be on PATH. `rocannon doctor` reports
anything missing.

## Quickstart

The quickstart profile targets `localhost` with `ansible_connection=local`.

```bash
cd examples/quickstart
rocannon mcp doctor --profile profile.yml   # list registered tools
rocannon repl       --profile profile.yml   # operator shell
```

Inside the REPL:

```
rocannon> .target localhost
rocannon> ping
rocannon> command cmd="uptime"
rocannon> .save my_session
rocannon> .exit
```

`.save` writes `.rocannon/playbooks/my_session.yml` as a standard Ansible
playbook. Run it directly with `ansible-playbook -i hosts my_session.yml`, or
let Rocannon load it back as an MCP prompt next time the server starts.

## CLI

```
rocannon mcp serve     start the MCP server (stdio or http)
rocannon mcp doctor    list registered tools, resources, prompts
rocannon repl          interactive shell on the same MCP server
rocannon run           call one module ad-hoc
rocannon doctor        system health (binaries, env, inventory)
rocannon doc <module>  print parsed schema for a module
rocannon search <q>    find modules by name or description
rocannon ls            list hosts/groups/modules from a profile
rocannon playbook      list/show/run saved playbooks
```

## Profiles

A profile is a YAML file declaring inventory + modules:

```yaml
inventories:
  - ./hosts
modules:
  - ansible.builtin
  - ibm.ibm_zos_core
ansible_cfg: ./ansible.cfg          # optional
vault_password_file: ~/.vault_pass  # optional
extra_envvars:                      # optional
  ZOAU_HOME: /usr/lpp/IBM/zoautil
```

`modules` accepts a specific module (`ansible.builtin.copy`), a collection
(`ansible.builtin`), or a namespace (`community`).

Multiple profiles can live under `.rocannon/profiles/`:

```
.rocannon/profiles/
├── box1.yml
├── box2.yml
└── default.yml -> box1.yml
```

```bash
rocannon mcp serve                     # uses default.yml
rocannon mcp serve --profile box2
```

The active profile can be switched at runtime via three MCP tools:
`rocannon_list_profiles`, `rocannon_current_profile`, `rocannon_use_profile`.
The tool surface is the union of every profile's modules; a call to a module
that isn't in the active profile returns a structured error pointing at
`rocannon_use_profile`.

## MCP clients

A working `.mcp.json` ships at the repo root. Per-client snippets are in
[`examples/clients/`](examples/clients/).

| Client | Config location |
|---|---|
| Claude Code | `.mcp.json` at project root, or `claude mcp add` |
| Claude Desktop | macOS: `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Cursor | `.cursor/mcp.json` or `~/.cursor/mcp.json` |
| mcphost | `~/.mcphost.yml` or `--config <path>` |
| IBM Bob | `.bob/mcp.json` or `~/.bob/mcp_settings.json` |

All share the standard `mcpServers` envelope pointing at
`rocannon mcp serve --profile <your-profile.yml>`.

## Development

```bash
git clone https://github.com/msradam/rocannon.git
cd rocannon
uv sync
./tests/check.sh                # ruff format + lint + mypy + pytest
uv run pytest -m integration    # opt-in: spins up a real UBI9 container
```

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for how the pieces fit together.

## The name

Ursula K. Le Guin coined the word "ansible" in her 1966 novel *Rocannon's
World*. The gryphon is a nod to the Windsteeds that Rocannon and his
companions ride.

## Credits

- Gryphon icon: [Gryphon by Aleksei Kovalenko from Noun Project](https://thenounproject.com/icon/gryphon-7096619/) (CC BY 3.0).
