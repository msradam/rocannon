# Rocannon

<p align="center">
  <img src="docs/assets/gryphon.svg" alt="" width="120">
</p>

<p align="center">
  <strong>The same Ansible your team runs by hand. Now callable from any AI agent.</strong>
</p>

<p align="center">
  <em>Every module — across every collection you've installed — becomes a typed MCP tool. Every AI session saves as a reviewable playbook your team can re-run by hand.</em>
</p>

Rocannon is an MCP server that reads `ansible-doc` at startup and registers
every installed Ansible module as a typed MCP tool. One server, every module,
auto-discovered from upstream.

![demo](docs/assets/demo.gif)

## What it is

Rocannon registers Ansible modules as MCP tools. It reads `ansible-doc -j` for
each module and builds typed function definitions. An MCP client (Claude
Desktop, Cursor, mcphost, any custom agent) sees them as ordinary tools with
required arguments, types, and descriptions.

Why typed tools instead of code generation:

- LLMs cannot invent argument names that the schema rejects.
- Required arguments are enforced by Pydantic before the call is dispatched.
- Results come back as structured JSON, not parsed text.
- Every module from every installed collection works the same way. No
  per-module glue code.

It also works without an LLM. Rocannon ships a REPL that calls the same MCP
server in-process. Tab completion, structured output, history.

## Install

```bash
# Core install (useful for `rocannon doctor`):
pip install rocannon

# Add Ansible (pulls in ansible-core + ansible-runner):
pip install 'rocannon[ansible]'

# Everything (litellm for AI mode + OTel exporters):
pip install 'rocannon[all]'
```

System binaries (`ansible-doc`, `ansible-runner`) are detected at startup. If
one is missing, `rocannon` exits with the install command for your platform.

## Quickstart

Runs against localhost. No SSH, no cloud.

```bash
# 1. From the rocannon checkout:
cd examples/quickstart

# 2. Check the toolchain is happy
rocannon mcp doctor --profile profile.yml

# 3. Start the operator REPL
rocannon repl --profile profile.yml
```

Inside the REPL:

```
rocannon> .target localhost
rocannon> ping
rocannon> command cmd="uptime"
rocannon> .history
rocannon> .save quickstart_session
rocannon> .exit
```

After `.save`, the session is persisted to `.rocannon/playbooks/quickstart_session.yml`
and loads back as an MCP prompt next time the server starts.

The same profile also works as an MCP server:

```bash
rocannon mcp serve --profile profile.yml
```

## CLI

```
rocannon mcp serve     start the MCP server (stdio or http transport)
rocannon mcp doctor    construct the server in-process and list its tools/resources/prompts
rocannon repl          interactive shell, drives the in-process MCP server
rocannon run           call one module ad-hoc (no MCP server, no REPL)
rocannon doctor        general system health (binaries, env vars, inventory parses)
rocannon doc <module>  print parsed schema for an Ansible module
rocannon search <q>    grep Ansible modules by name or description
rocannon ls            list hosts/groups/modules from a profile
rocannon playbook      list/show/run saved playbooks
```

Run `rocannon --help` or `rocannon <command> --help` for details.

## Profiles

A profile is a YAML file declaring which inventory and modules to expose.

```yaml
inventories:
  - ./hosts
modules:
  - ansible.builtin
  - community.general
  - ibm.ibm_zos_core
ansible_cfg: ./ansible.cfg          # optional
vault_password_file: ~/.vault_pass  # optional
extra_envvars:                      # optional: passed through to ansible-runner
  ZOAU_HOME: /usr/lpp/IBM/zoautil
```

A `modules` entry can be:

- A specific module: `ansible.builtin.copy`
- A collection: `ansible.builtin` (registers every module in the collection)
- A namespace: `community` (every collection in the namespace)

### Multiple profiles + runtime switching

Drop multiple profiles in `.rocannon/profiles/` at your project root (or
under `~/.rocannon/profiles/` for a user-level default):

```
my-project/
└── .rocannon/
    └── profiles/
        ├── dev.yml           # local dev inventory
        ├── box1.yml          # BOX1 sysplex
        ├── box2.yml          # BOX2 sysplex
        └── default.yml       # symlink → box1.yml
```

Rocannon walks up from CWD looking for `.rocannon/profiles/`, loads
everything in there, and uses `default.yml` (symlink, copy, or sole profile)
as the active profile at boot. Override with `--profile <name>`:

```bash
rocannon mcp serve                     # uses default.yml
rocannon mcp serve --profile box2      # boots with box2 active
rocannon mcp serve --profile ./one.yml # explicit path also works
```

While the server is running, three MCP tools let an LLM or operator switch
the active profile mid-session:

- `rocannon_list_profiles` — enumerate available profiles
- `rocannon_current_profile` — what's active now (inventory, modules, etc.)
- `rocannon_use_profile(name)` — switch the active profile

After switching, subsequent Ansible tool calls use the new profile's
inventory, `ansible_cfg`, vault, and envvars. The tool surface is the
**union** of every loaded profile's modules; calling a module that isn't in
the active profile returns a clean structured error pointing at
`rocannon_use_profile`.

## MCP clients

Rocannon speaks the standard MCP protocol over stdio or HTTP. Any MCP client
works. A working `.mcp.json` ships at the repo root; running `claude` from a
checkout auto-discovers it.

Per-client config snippets (all targeting the quickstart profile) live in
[`examples/clients/`](examples/clients/):

| Client | Where the config goes |
|---|---|
| **Claude Code** (CLI) | `.mcp.json` at the project root (shipped), or `claude mcp add ...` |
| **Claude Desktop** | macOS: `~/Library/Application Support/Claude/claude_desktop_config.json` |
| **Cursor** | `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global) |
| **mcphost** (terminal) | `~/.mcphost.yml` or `--config <path>` |
| **IBM Bob** (Shell + IDE) | `.bob/mcp.json` (project) or `~/.bob/mcp_settings.json` (global) |

All share the standard `mcpServers` envelope:

```json
{
  "mcpServers": {
    "rocannon": {
      "command": "uv",
      "args": [
        "run", "--directory", "/path/to/rocannon",
        "rocannon", "mcp", "serve",
        "--profile", "/path/to/rocannon/examples/quickstart/profile.yml"
      ]
    }
  }
}
```

Replace `/path/to/rocannon` with your checkout path. Use
`ROCANNON_DATA_DIR=<some-dir>` in the `env` field if you want `save_playbook`
to write outside the process CWD.

### Verify any client can spawn rocannon

```bash
claude mcp get rocannon          # Claude Code
mcphost --config examples/clients/mcphost.json --model ollama:granite4.1:3b \
  -p "list every tool you have"  # mcphost
```

Both should report the server connected and list every registered Ansible
module as a typed tool.

## REPL (non-AI operator mode)

The REPL is a plain shell for the same MCP tools an LLM would see. Useful when
you want to call modules directly without an LLM in the loop.

```
rocannon> .help                    show commands
rocannon> .target webhosts         set a default target
rocannon> .inventory               list hosts and groups
rocannon> .modules                 list every registered tool
rocannon> .doc copy                show the schema for ansible.builtin.copy
rocannon> .history                 recent calls this session
rocannon> .save my_runbook         persist this session as a playbook
rocannon> .ai <prompt>             optional: drive the same tools via litellm
rocannon> .exit                    leave (also ctrl-d)
```

Module calls use shell-style key=value syntax with shlex quoting:

```
rocannon> ansible.builtin.command target=h1 cmd="systemctl status nginx"
```

Short names resolve to FQCN, preferring `ansible.builtin`:

```
rocannon> ping target=h1            # → ansible.builtin.ping
```

The `.ai` mode is optional and off by default. It uses litellm so the backend
is up to the operator (Ollama, OpenAI, Anthropic, watsonx, vLLM, etc., picked
via `ROCANNON_AI_MODEL`).

## How it works

`rocannon.ansible.register_ansible_modules` reads schemas from `ansible-doc`,
builds typed function signatures, and registers each module on the FastMCP
server. Cross-cutting services apply to every tool: audit middleware,
correlation IDs, response size limits, retry on transient errors, history
buffer for save/replay.

Ansible is the only engine. Rocannon is not a plugin host for other
infrastructure tools.

## Saved playbooks

Two tools register at the server level:

- `save_playbook(name, description, steps, overwrite)` writes a playbook YAML
  to `$ROCANNON_DATA_DIR/.rocannon/playbooks/<name>.yml`.
- `commit_session(name, description, since)` materializes this session's
  successful tool calls into a playbook.

On the next server start, every saved playbook becomes an MCP prompt named
`playbook_<name>`. Step format is `{tool, args}` — the same shape an
LLM-driven session produces.

If `ansible-doc` shape changes between save and load (collection upgrade,
module rename), the playbook is skipped with a warning, not registered as a
half-broken prompt.

## Development

```bash
git clone https://github.com/msradam/rocannon.git
cd rocannon
uv sync                            # installs all dev deps
./tests/check.sh                   # ruff format + ruff check + mypy + pytest
./tests/check.sh --fix             # same, with auto-fix on format and lint
uv run pytest -m integration       # opt-in: spins up a real UBI9 container
```

The integration suite is opt-in because it builds and runs real containers.
Prereqs (any missing auto-skips the test):

- Docker daemon reachable
- `ansible-doc` on PATH

## The name

Ursula K. Le Guin coined the word "ansible" in her 1966 novel *Rocannon's
World*. Rocannon was the title character. Calling the engine a "cannon" is a
small pun on the name.

The gryphon at the top is a nod to the Windsteeds that Rocannon and his
companions ride in the novel.

## Credits

- Gryphon icon: [Gryphon by Aleksei Kovalenko from Noun Project](https://thenounproject.com/icon/gryphon-7096619/) (CC BY 3.0).
