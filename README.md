# Rocannon

<p align="center">
  <img src="docs/assets/gryphon.svg" alt="" width="120">
</p>

<p align="center">
  <strong>Every installed Ansible module as a typed MCP tool.</strong>
</p>

<p align="center">
  <em>Auto-discovered from <code>ansible-doc</code>. Every AI session saves as a reviewable playbook your team can re-run by hand.</em>
</p>

![demo](docs/assets/demo.gif)

## Install

```bash
pip install 'rocannon[ansible]'
```

`ansible-doc` and `ansible-runner` must be on PATH. `rocannon doctor` reports
anything missing.

## Quickstart

Runs against localhost. No SSH, no cloud.

```bash
cd examples/quickstart
rocannon mcp doctor --profile profile.yml   # toolchain check
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

`.save` writes `.rocannon/playbooks/my_session.yml`. It loads back as an MCP
prompt the next time the server starts.

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

Drop multiple profiles under `.rocannon/profiles/` and they auto-discover:

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

While the server runs, three MCP tools let you switch the active profile
without restarting: `rocannon_list_profiles`, `rocannon_current_profile`,
`rocannon_use_profile`. The tool surface is the union of every profile's
modules; calls to modules outside the active profile return a structured
error pointing at `rocannon_use_profile`.

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
