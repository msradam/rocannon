<h1 align="center">Rocannon</h1>

<p align="center">
  <img src="https://raw.githubusercontent.com/msradam/rocannon/main/docs/assets/gryphon.svg" alt="" width="120">
</p>

Rocannon turns any Ansible control node into an MCP server: it registers every
installed module as a typed tool. It reads `ansible-doc -j` for each module at
startup and builds a Pydantic-validated function signature, then exposes the
result over the MCP protocol (stdio or HTTP). The tool surface is whatever you
have installed, from one collection to a hundred. Any MCP client (Claude Code,
Cursor, mcphost, custom agents) calls the same tools an operator would call from
a REPL.

Each registered tool carries the module's own `ansible-doc` metadata: a JSON
output schema for structured results, and MCP safety hints derived from the
module's attributes (read-only for fact modules, destructive and open-world for
`command`, `shell`, `script`, and `raw`).

Every module is also a top-level CLI subcommand:

```
rocannon ansible.builtin.command --target h1 -i hosts --cmd 'uptime'
rocannon ansible.builtin.copy    --target h1 -i hosts --src /etc/hosts --dest /tmp/h
```

Each invocation needs an inventory: pass `-i/--inventory`, `--profile <name|path>`,
or run from a directory with a discovered `.rocannon/profiles/`.

Append `--record path/to/runbook.yml` to any invocation and Rocannon writes
each call as a new play in a real Ansible playbook. The resulting file runs
directly under `ansible-playbook -i <inv> path/to/runbook.yml`.

Add `--check` to preview a change without applying it (Ansible check mode) and
`--diff` to see what would change. Each is offered per module according to its
declared check-mode support, both on the CLI and as a parameter on the matching
MCP tool. `rocannon playbook run <name> --check` previews an entire saved runbook.

Sessions driven via the MCP server save the same way: as Ansible playbooks
under `.rocannon/playbooks/`. Rocannon also loads them back on next startup
as MCP prompts.

![demo](https://raw.githubusercontent.com/msradam/rocannon/main/docs/assets/demo-agent.gif)

Claude Haiku, via the Claude Agent SDK, driving Rocannon's typed Ansible-module
tools against a real RHEL 9 host in natural language.

### Case studies

- [`examples/case-study`](examples/case-study/) drives natural language into
  ad-hoc Ansible on a real RHEL 9 host: facts, a command, a config change, plus
  `--check` and replay as standard `ansible-playbook`.
- [`examples/containerlab`](examples/containerlab/) runs the same agent against a
  two-node Arista cEOS fabric, where the `arista.eos` modules become MCP tools.
- [`examples/execution-environment`](examples/execution-environment/) bakes
  Rocannon into an Ansible Execution Environment for a frozen, reproducible tool set.

## Install

```bash
pip install rocannon
```

This brings ansible-core and ansible-runner with it. `rocannon doctor` reports
anything missing from the environment.

## Quickstart

```bash
rocannon quickstart
```

This scaffolds a `localhost` profile (`ansible_connection=local`) under
`.rocannon/` and prints the exact wiring for your MCP client (Claude Code,
Claude Desktop, Cursor) plus a `rocannon mcp doctor` command to confirm the
tools register. Then ask your assistant something like *"Gather facts from
localhost and tell me the OS and kernel version."*

To explore from a shell instead of an MCP client:

```bash
rocannon mcp doctor --profile .rocannon/quickstart.yml   # list registered tools
rocannon repl       --profile .rocannon/quickstart.yml   # operator shell
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
rocannon quickstart    scaffold a localhost profile and print client wiring
rocannon <fqcn>        invoke a module: rocannon ansible.builtin.copy ...
                       optional --record FILE appends each call to a playbook
rocannon mcp serve     start the MCP server (stdio or http)
rocannon mcp doctor    list registered tools, resources, prompts
rocannon repl          interactive shell on the same MCP server
rocannon run           legacy ad-hoc form (module FQCN + -a key=value)
rocannon doctor        system health (binaries, env, inventory)
rocannon doc <module>  print parsed schema for a module (no profile needed)
rocannon search <q>    find modules by name or description
rocannon ls <kind>     list hosts, groups, or modules from a profile
rocannon playbook      list/show/run saved playbooks
```

Per-module help (typed flags, defaults, descriptions from `ansible-doc`):
`rocannon ansible.builtin.copy --help`. Modules that support check mode also
accept `--check` and `--diff`.

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
(`ansible.builtin`), or a namespace (`community`). Only Ansible modules become
tools; filter, lookup, and other plugin types are skipped. Listing specific
modules is much faster to start than a whole large collection, since each module
costs an `ansible-doc` call at startup.

Modules with third-party Python dependencies (for example `community.crypto`
needs `cryptography`, `community.docker` needs the `docker` SDK, `network_cli`
connections need `paramiko` or `ansible-pylibssh`) require those installed in
the same environment as Rocannon. The scaffolded quickstart inventory pins
`ansible_python_interpreter` to that environment so localhost runs use it; set
the same for other hosts if their modules need extra libraries.

Process environment variables do not carry into a local-connection task, so
connection details have to be passed as module arguments. For example
`community.docker` on a non-default socket (Colima, OrbStack, rootless Podman)
needs the socket as the module's `docker_host` argument, not `DOCKER_HOST`.

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

Rocannon is developed with AI assistance.

## The name

Ursula K. Le Guin coined the word "ansible" in her 1966 novel *Rocannon's
World*. The gryphon is a nod to the Windsteeds that Rocannon and his
companions ride.

## Credits

- Gryphon icon: [Gryphon by Aleksei Kovalenko from Noun Project](https://thenounproject.com/icon/gryphon-7096619/) (CC BY 3.0).
