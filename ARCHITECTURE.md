# Architecture brief

A plain-English tour of how rocannon actually works. Written for the author's
own benefit and for future contributors (human or AI) who want the mental
model behind the code without grepping their way through 4,000 lines.

## The 30-second version

Rocannon is a Python program that, at startup, walks three upstream catalogs,
reads their schemas, and turns every operation in them into a typed Python
function. It then hands those functions to FastMCP, which exposes them as MCP
tools over stdio or HTTP.

The three catalogs are:

| Catalog | What rocannon reads | Tool name shape |
|---|---|---|
| Ansible | `ansible-doc -j <module>` | `ansible.builtin.copy`, `community.general.docker_container` |
| Terraform | `tofu providers schema -json` + `variables.tf` for community modules | `tf_docker_container`, `tf_module_aws_vpc` |
| Helm | `helm show chart` | `helm_install_bitnami_nginx`, `helm_list` |

That's it. No bundled LLM, no opinionated provider matrix, no inventory
manager, no policy engine. Rocannon's job is the glue between "what upstream
ships" and "what an MCP client can call."

## The cannons abstraction

A *cannon* is the registration layer for one catalog. Three cannons ship:
`AnsibleCannon`, `TerraformCannon`, `HelmCannon`, all in
`src/rocannon/cannons/`.

Every cannon implements one method:

```python
class Cannon(ABC):
    def register(self, mcp: FastMCP, services: CannonServices) -> CannonMetrics:
        ...
```

The method reflects schemas from the upstream catalog, builds a typed Python
function per operation, registers each function with FastMCP via
`mcp.tool(...)`, and returns a `CannonMetrics` record so the doctor knows
what got loaded.

Adding a fourth cannon (say, Kubernetes-via-kubectl) means subclassing
`Cannon` and implementing one method. Nothing else in the codebase needs to
know about the new cannon; `server.create_server()` iterates whatever
cannons the profile enabled.

Cross-cutting concerns (audit logging, correlation IDs, response size
limits, transient-error retry, the history buffer feeding save/replay) live
in `server.py` and reach the cannons through `CannonServices`. A cannon
should never reimplement these.

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
│ Typed tool fn   │  Built at startup by AnsibleCannon
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
   Ansible module name, the resolved inventory, and the rocannon config. When
   called, it hands the args to the executor.
5. **The executor** (`src/rocannon/executor.py`) uses ansible-runner's
   Python API to invoke `ansible-playbook` as a subprocess. It synthesises
   a one-task playbook from the module name and args, runs it against the
   target host or group, parses the JSON event stream, and returns a
   structured dict.
6. The result bubbles back up through the middleware (audit record gets the
   latency + status + any redacted error), FastMCP serialises it to MCP's
   tool-result format, and the client gets structured JSON.

The same shape applies to Terraform and Helm calls. The executor changes
(tofu / helm subprocesses instead of ansible-runner), but the registration
flow and middleware are identical.

## Each cannon, in detail

### AnsibleCannon

`src/rocannon/cannons/ansible.py` (106 lines, the simplest).

**What it reflects.** Whatever modules the profile asked for, expanded
through `src/rocannon/schema.py`. A spec can be a fully-qualified collection
name (`ansible.builtin.copy`), a collection (`ansible.builtin`, expanded to
every module), or a namespace (`ansible`, expanded across collections).

For each module, the cannon runs `ansible-doc -j <module>` as a subprocess
and parses the JSON. The parser pulls out parameter names, types, required
flags, choices, and descriptions.

**What it exposes.** One tool per module. Tool name is the module's FQCN.
Tool parameters mirror the module's documented parameters, with one addition:
a `target` parameter (the inventory host or group pattern).

**Quirks.**
- ansible-doc is slow (around 300ms per module on a warm machine). For
  collections with hundreds of modules this dominates startup time.
  Loading specific modules instead of whole collections is the fastest path.
- Some module parameters have names that collide with Python keywords (`if`,
  `from`) or with reserved cannon slots (`target`). The cannon mangles those
  on the way in via `_sanitize_param_name` and de-mangles on the way out.

**Resources.** AnsibleCannon also registers MCP resources for inventory
introspection (`inventory://hosts`, `inventory://groups`) and per-module
schema dumps (`module://<fqcn>/schema`).

### TerraformCannon

`src/rocannon/cannons/terraform.py` (1024 lines, by far the largest, because
Terraform's schema model is the most layered).

**What it reflects.** Two distinct things:

1. **Provider resources.** For each provider declared in the profile (say
   `kreuzwerker/docker`), the cannon writes a `providers.tf.json` to the
   workspace, runs `tofu init`, then `tofu providers schema -json`, and
   parses the resulting JSON. The schema gives every resource type that
   provider supports, with all attributes and types.
2. **Community registry modules.** For each module in the profile (say
   `cloudposse/label/null`), `tofu init` downloads it into
   `.terraform/modules/<key>/`. The cannon parses the module's
   `variables.tf` with python-hcl2 to extract input variables, since
   provider schemas don't cover modules.

**What it exposes.** One tool per resource type (`tf_docker_container`,
`tf_aws_instance`, ...). One tool per module (`tf_module_null_label`).
Plus a small handful of meta tools: `tf_apply`, `tf_destroy`,
`tf_workspace_status`.

**Quirks.**
- Terraform's type system maps to Python through a custom translator
  (`_terraform_type_to_python`). Lists and maps come through as nested
  schemas like `["list", "string"]`; the translator handles arbitrary
  nesting and synthesises Pydantic-callable annotations.
- Each resource tool needs an `instance` parameter (the local block name in
  generated HCL). If a resource type has a literal attribute called
  `instance`, the cannon mangles it. Same trick as the Ansible cannon's
  parameter sanitiser.
- For community modules, pure-computation modules (like cloudposse/label)
  don't store anything in state because they have no real resources.
  Rocannon synthesises `output` blocks in the workspace so the module's
  computed values land in state and become available to subsequent calls.
- `tofu` returns exit code 2 from `plan` to mean "changes pending,"
  distinct from 0 (no changes) and 1 (error). The cannon treats 2 as
  success.
- Every mutating call wraps itself in a revert-on-failure: if `apply`
  errors, the resource block (or module block + outputs) is restored to
  its pre-call state so the workspace stays consistent.

This cannon is large because Terraform's reflection requires multiple
subprocess invocations, JSON + HCL parsing, two distinct schema sources
(providers and modules), and careful workspace state management. The other
two cannons are simpler because their upstream catalogs are flatter.

### HelmCannon

`src/rocannon/cannons/helm.py` (316 lines).

**What it reflects.** For each chart in the profile (say
`bitnami/nginx@21.0.6`), the cannon runs `helm show chart` and `helm show
values`. The first gives chart metadata; the second gives the default
values YAML, which becomes the schema for the install tool.

**What it exposes.** One install tool per chart (`helm_install_bitnami_nginx`),
plus chart-agnostic meta tools (`helm_list`, `helm_status`, `helm_uninstall`,
`helm_repo_add`).

**Quirks.**
- Helm values are nested YAML, not a flat schema. The cannon exposes the
  full nested structure as a Pydantic-validated `values` parameter rather
  than synthesising one tool argument per leaf value, which would be
  unworkable for charts with hundreds of options.
- `helm_status` returns the full release manifest including all rendered
  Kubernetes YAML. Smaller models (Granite 3B was the canary) sometimes
  start generating ingress tutorials when they see this. Demos prefer
  `helm_list` for that reason; production callers can use either.

## The MCP server layer

`src/rocannon/server.py` (740 lines) builds the FastMCP server and wires the
middleware stack. The middleware order matters: each layer wraps the next,
inside-out.

```
request in:
    correlation ID assigned  ──┐
        structured log emitted ──┐
            audit record opened   ──┐
                response-limit applied ──┐
                    retry policy active   ──┐
                        cannon handler runs ──┘
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

`src/rocannon/repl.py` (471 lines) is a prompt-toolkit shell that drives the
same MCP server in-process. It is not a separate code path; it constructs a
FastMCP server identically to `mcp serve`, then calls into it without a
JSON-RPC transport in the middle.

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

## Cross-cannon save/replay (playbooks)

A *rocannon playbook* (distinct from an Ansible playbook) is a YAML file
recording a sequence of MCP tool calls. The model is generic:

```yaml
name: nightly-stack
description: Bring up the demo stack
steps:
  - tool: tf_docker_network
    args: {instance: demo_net, name: demo-net}
  - tool: helm_install_bitnami_redis
    args: {release_name: cache, namespace: demo}
  - tool: ansible.builtin.command
    args: {target: webhosts, cmd: systemctl restart nginx}
```

Steps can mix any cannons. The runtime doesn't care which cannon registered
the tool, only that the tool name resolves at replay time.

Two server-level tools handle this:

- **`save_playbook(name, description, steps, overwrite)`** writes the YAML
  to `$ROCANNON_DATA_DIR/.rocannon/playbooks/<name>.yml`.
- **`commit_session(name, description, since)`** materialises the current
  session's successful tool calls (from the history buffer in
  `src/rocannon/history.py`) into a playbook.

On the next server start, every saved playbook is loaded as an MCP prompt
named `playbook_<name>`, so an MCP client can list and replay them.

If a playbook references a tool that's no longer registered (provider
upgrade, module rename, cannon disabled), it's skipped with a warning. The
runtime never registers a half-broken prompt.

## Configuration loading

A *profile* is a YAML file declaring which cannons to load and what they
should expose. See `examples/quickstart/profile.yml` for the canonical
shape; `examples/profiles/` has one per scenario.

`src/rocannon/config.py` loads profiles with Pydantic and handles one
non-obvious thing: **paths in a profile resolve against the profile file's
parent directory, not the process CWD**. This is what makes `claude mcp add
... --profile examples/quickstart/profile.yml` work, where the profile
references `./hosts` and `./tf-work` even though Claude Code spawns
rocannon from a CWD that has nothing to do with the profile location.

## What's deliberately NOT in scope

These show up in design discussions and the answer is "no":

- **No bundled LLM.** Rocannon is an MCP server. Pick your own client.
- **No opinionated provider matrix.** LiteLLM handles backend selection in
  `.ai` mode; rocannon doesn't ship a "blessed" model list.
- **No inventory management UI.** Ansible inventories are YAML/INI files,
  same as always. Rocannon reads them.
- **No policy engine.** Authorisation is the MCP client's responsibility.
  IBM Bob's `alwaysAllow` field is one example.
- **No OpenAPI-to-MCP path.** FastMCP already does this. Rocannon's
  contribution is the typed-tools-from-Ansible-style-catalogs path.
- **No state management beyond what upstream provides.** Terraform state
  lives in the workspace. Helm release tracking lives in Kubernetes. We
  don't shadow either.

## Code map

```
src/rocannon/
├── cli.py              Typer entrypoint. Subcommands: mcp serve|doctor,
│                       repl, run, doctor, doc, search, ls, playbook.
├── config.py           Pydantic Config model + YAML profile loader.
├── server.py           create_server(). Iterates cannons, wires middleware,
│                       registers cross-cannon save_playbook + commit_session.
├── schema.py           ansible-doc parsing, module spec expansion.
├── executor.py         ansible-runner Python-API wrapper.
├── playbook.py         Cross-cannon playbook model {tool, args}.
├── repl.py             Operator REPL + optional .ai mode (LiteLLM).
├── inventory.py        ansible-inventory subprocess wrapper.
├── history.py          In-memory ring buffer feeding save/replay.
├── correlation.py      Request correlation IDs for the audit log.
├── redaction.py        Secrets redaction in audit records.
└── cannons/
    ├── __init__.py     Cannon ABC, CannonServices, CannonMetrics.
    ├── ansible.py      AnsibleCannon.
    ├── terraform.py    TerraformCannon (the big one).
    └── helm.py         HelmCannon.

tests/
├── conftest.py                       Container + Ollama fixtures.
├── containers/Containerfile.ubuntu   The integration-test target.
├── test_unit.py                      Fast unit tests, no infra.
├── test_collections.py               Ansible collection expansion.
├── test_server.py                    FastMCP server construction.
├── test_cannons_integration.py       Real Ansible + Terraform + Helm.
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

**Integration tests (`pytest -m integration`).** Spin up a real Ansible
target container, a real OpenTofu workspace, a real kind Kubernetes
cluster. Verify that every cannon's tool actually executes end-to-end.
Opt-in because they require docker, tofu, helm, and a kind cluster named
`rocannon-test`. CI does not run them.

The combined gate is `./tests/check.sh`: ruff format, ruff lint, mypy
strict, pytest. The same chain runs in `.github/workflows/ci.yml`.

## Known sharp edges

Things that surprised the author while building this:

- **ansible-doc is slow.** Loading whole collections at startup adds seconds
  to seconds-per-collection. Production setups should list specific modules.
- **Terraform's `tofu init` cache is the difference between sub-second and
  minute-long startup.** Don't blow away `.terraform/` between server
  restarts unless you have to.
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
- **The Helm `_status` tool returns rendered Kubernetes YAML.** Useful to a
  human, distracting to a small model. Use `helm_list` for terse status.

## Where to start when debugging

In rough order of probability:

1. **Tool registration failed at startup.** Run `rocannon mcp doctor
   --profile <p>`. It constructs the server in-process and reports what
   loaded, what didn't, and why.
2. **Tool call fails at runtime.** Check the audit log (`rocannon.audit`
   logger). Each call has a correlation ID and a structured error.
3. **Schema looks wrong.** `rocannon doc <module>` shows what rocannon
   parsed from upstream. Compare against `ansible-doc <module>`,
   `tofu providers schema`, or `helm show chart` directly.
4. **MCP client can't see rocannon.** From the project root,
   `claude mcp get rocannon` should report `Status: ✓ Connected`. If not,
   the project-level `.mcp.json` is the first place to look.
5. **Integration test fails on docker / tofu / helm.** Confirm the binaries
   are on PATH and the kind cluster named `rocannon-test` exists. The
   conftest auto-skips when prereqs are missing; an actual failure means
   something inside the cannon is wrong, not the test harness.
