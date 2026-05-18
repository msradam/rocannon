# Architecture brief

A plain-English tour of how rocannon actually works. Written for the author's
own benefit and for future contributors (human or AI) who want the mental
model behind the code without grepping their way through the source tree.

## The 30-second version

Rocannon is a Python program that, at startup, walks the Ansible module
catalog, reads each module's schema via `ansible-doc -j`, and turns every
module into a typed Python function. It then hands those functions to FastMCP,
which exposes them as MCP tools over stdio or HTTP.

| Source | What rocannon reads | Tool name shape |
|---|---|---|
| Ansible | `ansible-doc -j <module>` | `ansible.builtin.copy`, `community.general.docker_container`, `ibm.ibm_zos_core.zos_data_set` |

That's it. No bundled LLM, no opinionated provider matrix, no inventory
manager, no policy engine, no plugin abstraction for other tools. Rocannon's
job is the glue between "what ansible-doc ships" and "what an MCP client
can call."

## End-to-end: what happens when you call one tool

Concrete example: the user types in their MCP client

```
ansible.builtin.command(target="webhosts", cmd="systemctl restart nginx")
```

What actually runs:

```
┌─────────────────┐
│ MCP client      │  e.g. Claude Code, mcphost, IBM Bob
│ (Claude Code,   │
│  mcphost, Bob)  │
└────────┬────────┘
         │ stdio JSON-RPC
         ▼
┌─────────────────┐
│ FastMCP server  │  src/rocannon/server.py
│ (rocannon proc) │  - middleware stack runs:
│                 │    1. correlation ID
└────────┬────────┘    2. structured logging
         │             3. audit record (with redaction)
         │             4. response size limit
         │             5. retry on transient errors
         ▼
┌─────────────────┐
│ Typed tool fn   │  Built at startup by register_ansible_modules
│ (closure over   │  from `ansible-doc -j ansible.builtin.command`
│  module schema) │
└────────┬────────┘
         │ subprocess
         ▼
┌─────────────────┐
│ ansible-runner  │  Spawns ansible-playbook against the inventory,
│ (Python API)    │  parses the JSON event stream, returns a structured
│                 │  result dict.
└─────────────────┘
```

The pieces, in order:

1. **The MCP client** sends a JSON-RPC `tools/call` over stdio. It looks
   exactly like any other MCP call. The client doesn't know rocannon is
   wrapping Ansible; it just sees a typed tool with declared parameters.
2. **FastMCP** routes the call to the registered handler. Pydantic validates
   the arguments against the schema rocannon built at startup. Missing
   required args fail here, not at execution time.
3. **The middleware stack** runs in order: it attaches a correlation ID, logs
   a structured request record, prepares the audit entry (which will be
   completed when the response comes back), enforces a max response size,
   and wraps the handler in a retry policy for transient transport-level
   exceptions.
4. **The typed tool function** is a closure built at startup. It captures the
   Ansible module name and the runtime context. When called, it reads the
   active profile's inventory/envvars/timeouts from the runtime, then hands
   the args to the executor.
5. **The executor** (`src/rocannon/executor.py`) uses ansible-runner's
   Python API to invoke `ansible-playbook` as a subprocess. It synthesises
   a one-task playbook from the module name and args, runs it against the
   target host or group, parses the JSON event stream, and returns a
   structured dict.
6. The result bubbles back up through the middleware (audit record gets the
   latency + status + any redacted error), FastMCP serialises it to MCP's
   tool-result format, and the client gets structured JSON.

## Module reflection and registration

`src/rocannon/ansible.py` is where every Ansible-specific concern lives.

**What it reflects.** Whatever modules the profile asked for, expanded
through `src/rocannon/schema.py`. A spec can be a fully-qualified module
name (`ansible.builtin.copy`), a collection (`ansible.builtin`, expanded to
every module), or a namespace (`ansible`, expanded across collections).

For each module, `register_ansible_modules` runs `ansible-doc -j <module>` as
a subprocess and parses the JSON. The parser pulls out parameter names,
types, required flags, choices, and descriptions.

**What it exposes.** One tool per module. Tool name is the module's FQCN.
Tool parameters mirror the module's documented parameters, with one addition:
a `target` parameter (the inventory host or group pattern).

**Quirks.**
- ansible-doc is slow (around 300ms per module on a warm machine). For
  collections with hundreds of modules this dominates startup time.
  Loading specific modules instead of whole collections is the fastest path.
- Some module parameters have names that collide with Python keywords (`if`,
  `from`) or with reserved slots (`target`). The registration layer mangles
  those on the way in via `_sanitize_param_name` and de-mangles on the way
  out.

**Resources.** The Ansible layer also registers
`rocannon://inventory` (active profile's hosts + groups) and
`rocannon://module/<fqcn>` (parsed schema per module). The cross-cutting
`rocannon://runs` and `rocannon://runs/{request_id}` resources live in
`server.py`.

## The MCP server layer

`src/rocannon/server.py` builds the FastMCP server and wires the middleware
stack. The middleware order matters: each layer wraps the next, inside-out.

```
request in:
    correlation ID assigned  ──┐
        structured log emitted ──┐
            audit record opened   ──┐
                response-limit applied ──┐
                    retry policy active   ──┐
                        tool handler runs   ──┘
                    audit record completed
                structured log finalised
            correlation ID released
response out
```

The audit log lives in its own logger (`rocannon.audit`) so it can be
redirected separately from operational logs. Each record is a single JSON
line with tool name, args (with secrets redacted by
`src/rocannon/redaction.py`), correlation ID, latency, and status.

OpenTelemetry tracing is optional. If `opentelemetry-api` is importable, the
server adds a `tools/call <name>` span per call with attributes for module
name, target, and latency. If it isn't, the import fails silently and
tracing is off. No runtime cost when disabled.

## The REPL

`src/rocannon/repl.py` is a prompt-toolkit shell that drives the same MCP
server in-process. It is not a separate code path; it constructs a FastMCP
server identically to `mcp serve`, then calls into it without a JSON-RPC
transport in the middle.

This matters for two reasons:
- Whatever tools the operator can run from the REPL are exactly the same
  tools an LLM would see through MCP. There is no "REPL only" surface.
- The REPL is the no-AI mode. Tab completion, history, structured output,
  `.save` to persist a session as a playbook. An operator can use rocannon
  without ever attaching an LLM.

`.ai` mode in the REPL is optional. It uses LiteLLM, so the backend is up
to the operator (Ollama, OpenAI, Anthropic, watsonx, vLLM, anything LiteLLM
supports). The model name is read from `ROCANNON_AI_MODEL`. There is no
opinionated default.

## Save/replay (playbooks)

A *rocannon playbook* (distinct from an Ansible playbook) is a YAML file
recording a sequence of MCP tool calls:

```yaml
name: restart-stack
description: Restart the web tier and verify
steps:
  - tool: ansible.builtin.command
    args: {target: webhosts, cmd: systemctl restart nginx}
  - tool: ansible.builtin.wait_for
    args: {target: webhosts, host: 127.0.0.1, port: 80}
  - tool: ansible.builtin.uri
    args: {target: localhost, url: "http://web.example.com/healthz"}
```

Two server-level tools handle this:

- **`save_playbook(name, description, steps, overwrite)`** writes the YAML
  to `$ROCANNON_DATA_DIR/.rocannon/playbooks/<name>.yml`.
- **`commit_session(name, description, since)`** materialises the current
  session's successful tool calls (from the history buffer in
  `src/rocannon/history.py`) into a playbook.

On the next server start, every saved playbook is loaded as an MCP prompt
named `playbook_<name>`, so an MCP client can list and replay them.

If a playbook references a tool that's no longer registered (collection
upgrade, module rename), it's skipped with a warning. The runtime never
registers a half-broken prompt.

## Configuration loading

A *profile* is a YAML file declaring which inventory and modules to expose.
See `examples/quickstart/profile.yml` for the canonical shape;
`examples/profiles/` has one per scenario.

`src/rocannon/config.py` loads profiles with Pydantic and handles one
non-obvious thing: **paths in a profile resolve against the profile file's
parent directory, not the process CWD**. This is what makes `claude mcp add
... --profile examples/quickstart/profile.yml` work, where the profile
references `./hosts` even though Claude Code spawns rocannon from a CWD
that has nothing to do with the profile location.

### Profile discovery + runtime switching

`src/rocannon/profiles.py` adds discovery and a runtime registry on top of
single-profile loading.

- `discover_profiles_dir()` walks up from CWD looking for
  `.rocannon/profiles/`. Falls back to `~/.rocannon/profiles/`.
- `load_profile_registry(dir)` loads every `*.yml` in that directory, each
  under its filename stem. `default.yml` (symlink, regular file, or implicit
  if there's only one profile) sets the default-at-boot.
- `RuntimeContext` holds the active profile name plus an `asyncio.Lock`.
  `rocannon_use_profile(name)` mutates it; tool functions read
  `active_config()` on every call, so a switch takes effect immediately
  without re-registering tools.

`register_ansible_modules` registers the **union** of every profile's
modules once. The active profile is consulted at call time, not
registration. If the active profile doesn't declare the module being
called, the tool returns a structured error (`status: error`) pointing at
`rocannon_use_profile`, rather than failing inside ansible-runner.

The typed `target` annotation is built from the **union** of hosts and
groups across every loaded profile. Ansible itself validates the target
against the active inventory at execution time.

## What's deliberately NOT in scope

These show up in design discussions and the answer is "no":

- **No bundled LLM.** Rocannon is an MCP server. Pick your own client.
- **No opinionated provider matrix.** LiteLLM handles backend selection in
  `.ai` mode; rocannon doesn't ship a "blessed" model list.
- **No inventory management UI.** Ansible inventories are YAML/INI files,
  same as always. Rocannon reads them.
- **No policy engine.** Authorisation is the MCP client's responsibility.
  IBM Bob's `alwaysAllow` field is one example.
- **No Terraform / Helm / Kubernetes / Salt integration.** Those tools have
  different shapes (stateful workspaces, declarative manifests, release-as-
  unit) and dedicated MCP servers already exist for them. Rocannon stays
  narrow: every Ansible module, nothing else.
- **No OpenAPI-to-MCP path.** FastMCP already does this. Rocannon's
  contribution is the typed-tools-from-Ansible-catalog path.

## Code map

```
src/rocannon/
├── cli.py              Typer entrypoint. Subcommands: mcp serve|doctor,
│                       repl, run, doctor, doc, search, ls, playbook.
├── config.py           Pydantic Config model + YAML profile loader.
├── profiles.py         Profile discovery, registry, RuntimeContext (active
│                       profile + asyncio.Lock for runtime switching).
├── ansible.py          register_ansible_modules: ansible-doc reflection,
│                       typed tool registration, inventory + module resources.
├── server.py           create_server(). Wires FastMCP middleware, calls
│                       register_ansible_modules, registers save_playbook +
│                       commit_session + rocannon_{list,current,use}_profile.
├── schema.py           ansible-doc parsing, module spec expansion.
├── executor.py         ansible-runner Python-API wrapper.
├── playbook.py         Playbook model {tool, args}.
├── repl.py             Operator REPL + optional .ai mode (LiteLLM).
├── inventory.py        ansible-inventory subprocess wrapper.
├── history.py          In-memory ring buffer feeding save/replay.
├── correlation.py      Request correlation IDs for the audit log.
└── redaction.py        Secrets redaction in audit records.

tests/
├── conftest.py                       Shared fixtures.
├── containers/Containerfile.ubuntu   The integration-test target.
├── test_unit.py                      Fast unit tests, no infra.
├── test_collections.py               Ansible collection expansion.
├── test_server.py                    FastMCP server construction.
├── test_ansible_integration.py       Real UBI9 SSH container.
│                                     Opt-in via `pytest -m integration`.
└── check.sh                          The one quality-gate script.

dev/                                  Author's lab infrastructure.
├── inventories/                      IBM z/OS LPARs + Wazi cloud.
├── profiles/                         z/OS-targeted profiles.
└── tests/                            LinuxONE + z/OS + WatsonX integration.

examples/
├── quickstart/                       Canonical try-it-now: localhost only.
├── profiles/                         One per common scenario.
├── inventories/                      Inventories referenced by profiles.
└── clients/                          One MCP-client config per file.

docs/
├── assets/                           Logo + demo gif + asciinema cast.
└── recording/                        How to regenerate the demo gif.
```

## Testing strategy

Two tiers, separated by a pytest marker.

**Unit tests (default).** Fast, no external dependencies. Cover schema
parsing, collection expansion, config loading, playbook serialisation,
sanitisation rules. Run on every CI commit.

**Integration tests (`pytest -m integration`).** Spin up a real UBI9 SSH
container and verify Ansible modules execute end-to-end through the
registration layer. Opt-in because they require docker. CI does not run
them.

The combined gate is `./tests/check.sh`: ruff format, ruff lint, mypy
strict, pytest. The same chain runs in `.github/workflows/ci.yml`.

## Known sharp edges

Things that surprised the author while building this:

- **ansible-doc is slow.** Loading whole collections at startup adds seconds
  to seconds-per-collection. Production setups should list specific modules.
- **Small models generate tool calls as text.** Granite 3B occasionally
  emits `tool_name(arg=...)` as a string in its response instead of using
  the function-calling protocol. The natural fix is a larger model, but
  several prompt-shape changes in the REPL's `.ai` mode help (the working
  set is "use FQCNs verbatim, give one task at a time").
- **vhs produces blank GIFs on macOS Tahoe.** The demo recording pipeline
  uses asciinema + agg instead. See `docs/recording/README.md`.
- **mcphost ignores the `env` field in mcp.json.** Workaround:
  `command: "env"` + `args: ["VAR=value", "rocannon", ...]`. Other clients
  (Claude Code, Cursor, Bob) honour `env` correctly.

## Where to start when debugging

In rough order of probability:

1. **Tool registration failed at startup.** Run `rocannon mcp doctor
   --profile <p>`. It constructs the server in-process and reports what
   loaded, what didn't, and why.
2. **Tool call fails at runtime.** Check the audit log (`rocannon.audit`
   logger). Each call has a correlation ID and a structured error.
3. **Schema looks wrong.** `rocannon doc <module>` shows what rocannon
   parsed from upstream. Compare against `ansible-doc <module>` directly.
4. **MCP client can't see rocannon.** From the project root,
   `claude mcp get rocannon` should report `Status: ✓ Connected`. If not,
   the project-level `.mcp.json` is the first place to look.
5. **Integration test fails on docker.** Confirm docker is running. The
   conftest auto-skips when prereqs are missing; an actual failure means
   something inside the registration or execution path is wrong, not the
   test harness.
