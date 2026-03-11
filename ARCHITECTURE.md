# Rocannon Architecture

## Core Thesis

Every installed Ansible module can become an MCP tool — automatically, without any module-specific code.

This works because `ansible-doc` already ships a machine-readable JSON schema for every module: parameter names, types, required flags, choices, defaults, descriptions. Rocannon reads that schema, maps it 1:1 onto an MCP tool definition, and lets ansible-runner handle execution. The entire "intelligence" of what each module does lives inside Ansible itself. Rocannon is just a bridge.

```
┌─────────────┐     MCP Protocol      ┌──────────────┐
│  LLM Client │◄─────────────────────►│   Rocannon   │
│  (Claude,   │  tool definitions +   │  FastMCP     │
│   Ollama,   │  tool calls/results   │  Server      │
│   etc.)     │                       │              │
└─────────────┘                       └──────┬───────┘
                                             │
                          ┌──────────────────┼──────────────────┐
                          │                  │                  │
                          ▼                  ▼                  ▼
                   ┌────────────┐    ┌────────────┐    ┌────────────┐
                   │ansible-doc │    │  inventory  │    │ansible-    │
                   │  (schema)  │    │  (hosts)    │    │runner      │
                   └────────────┘    └────────────┘    │(execution) │
                                                       └─────┬──────┘
                                                             │ SSH
                                          ┌──────────────────┼──────────────┐
                                          ▼                  ▼              ▼
                                    ┌──────────┐     ┌──────────┐   ┌──────────┐
                                    │  Host A   │     │  Host B   │   │  Host C  │
                                    └──────────┘     └──────────┘   └──────────┘
```

## Pipeline

There are three phases: **startup** (schema loading), **registration** (tool creation), and **runtime** (tool execution). Each is deliberately simple.

### Phase 1: Startup — Schema Loading

```
profiles/local-dev.yml          cli.py                    config.py
        │                          │                          │
        │  --profile path          │                          │
        └─────────────────────────►│  load_profile(path)      │
                                   ├─────────────────────────►│
                                   │                          │  Config(
                                   │                          │    inventories=[...],
                                   │                          │    modules=[...],
                                   │                          │    transport="stdio"
                                   │                          │  )
                                   │◄─────────────────────────┤
                                   │                          │
                                   │  create_server(config)   │
                                   ├─────────────────────────►│
                                   │                     server.py
```

`config.py` validates with Pydantic: inventory files must exist on disk, at least one module spec is required.

### Phase 2: Registration — Module Expansion and Tool Creation

```
server.py                    inventory.py                 schema.py
    │                             │                           │
    │  load_inventories(paths)    │                           │
    ├────────────────────────────►│                           │
    │                             │  YAML parse + merge       │
    │  ◄── {host: vars, ...}  ───┤                           │
    │                             │                           │
    │  expand_modules(specs)      │                           │
    ├────────────────────────────────────────────────────────►│
    │                             │                           │
    │                             │    ansible-doc --list -j  │
    │                             │    ┌──────────────────────┤
    │                             │    │  subprocess          │
    │                             │    │  returns ALL modules │
    │                             │    └──────────────────────┤
    │                             │                           │
    │                             │    filter by prefix       │
    │                             │    "ansible.builtin." →   │
    │  ◄── [71 module names] ────────────────────────────────┤
    │                             │                           │
    │  for each module:           │                           │
    │    fetch_module_schema(mod) │                           │
    │  ──────────────────────────────────────────────────────►│
    │                             │                           │
    │                             │    ansible-doc -j <mod>   │
    │                             │    ┌──────────────────────┤
    │                             │    │  subprocess          │
    │                             │    │  returns full doc    │
    │                             │    └──────────────────────┤
    │                             │                           │
    │                             │    _parse_module_doc()    │
    │                             │    extract:               │
    │                             │      - short_description  │
    │                             │      - options → params   │
    │                             │      - required, type,    │
    │                             │        default, choices,  │
    │                             │        aliases,           │
    │                             │        suboptions,        │
    │                             │        deprecated         │
    │                             │                           │
    │  ◄── {name, desc, params} ─────────────────────────────┤
    │                             │                           │
    │  _register_tool(mcp, ...)   │                           │
    │  ┌──────────────────────┐   │                           │
    │  │ Build description:   │   │                           │
    │  │   short_description  │   │                           │
    │  │   + param docs       │   │                           │
    │  │                      │   │                           │
    │  │ mcp.tool(            │   │                           │
    │  │   name=module_name,  │   │                           │
    │  │   description=...,   │   │                           │
    │  │ )(tool_fn)           │   │                           │
    │  └──────────────────────┘   │                           │
```

The key transformation is `_parse_module_doc()`. Here's what it distills from ansible-doc's raw output:

```
RAW ansible-doc JSON (hundreds of lines)        Rocannon schema (compact)
─────────────────────────────────────────        ─────────────────────────
{                                                {
  "doc": {                                         "name": "ansible.builtin.copy",
    "attributes": { ... },          ← ignored      "description": "Copy files to remote locations",
    "author": [ ... ],              ← ignored      "parameters": [
    "notes": [ ... ],               ← ignored        {
    "seealso": [ ... ],             ← ignored          "name": "dest",
    "short_description": "Copy..",  ← USED             "description": "Remote absolute path...",
    "options": {                                       "required": true,
      "dest": {                                        "type": "path"
        "description": [...],       ← USED           },
        "required": true,           ← USED           {
        "type": "path"              ← USED             "name": "content",
      },                                               "description": "Sets contents directly...",
      "content": {                                     "required": false,
        "description": [...],       ← USED             "type": "str"
        "type": "str",              ← USED           },
        "aliases": ["data"],        ← USED           ...
      },                                           ]
      "backup": {                                }
        "default": false,           ← USED
        "type": "bool",             ← USED
        "deprecated": { ... },      ← USED (marked in desc)
      },
      "mode": {
        "type": "raw",              ← USED (passed through)
        "suboptions": { ... },      ← USED (flattened to desc)
      },
    }
  },
  "examples": "...",                ← ignored
  "return": { ... },                ← ignored
}
```

Fields used: `short_description`, `options.*` (name, description, required, default, choices, type, elements, aliases, deprecated, suboptions).

Fields ignored: `attributes`, `author`, `collection`, `filename`, `has_action`, `notes`, `seealso`, `version_added`, `examples`, `return`.

### Phase 3: Runtime — Tool Execution

```
LLM calls tool                     server.py                    executor.py
    │                                   │                            │
    │  call_tool(                       │                            │
    │    "ansible.builtin.copy",        │                            │
    │    {                              │                            │
    │      "host": "podman-alpine",     │                            │
    │      "module_args": {             │                            │
    │        "content": "hello",        │                            │
    │        "dest": "/tmp/test"        │                            │
    │      }                            │                            │
    │    }                              │                            │
    │  )                                │                            │
    ├──────────────────────────────────►│                            │
    │                                   │                            │
    │                              tool_fn()                         │
    │                                   │                            │
    │                         ┌─────────┤                            │
    │                         │ HOST    │                            │
    │                         │ CHECK:  │                            │
    │                         │ "podman-alpine"                      │
    │                         │ in valid_hosts?                      │
    │                         │   YES → continue                     │
    │                         │   NO  → return {status: "rejected"}  │
    │                         └─────────┤                            │
    │                                   │                            │
    │                                   │  run_module(               │
    │                                   │    module="a.b.copy",      │
    │                                   │    module_args={...},      │
    │                                   │    inventory=[paths],      │
    │                                   │    host_pattern="podman-a" │
    │                                   │  )                         │
    │                                   ├───────────────────────────►│
    │                                   │                            │
    │                                   │               ┌────────────┤
    │                                   │               │ Args logic:│
    │                                   │               │            │
    │                                   │               │ free-form? │
    │                                   │               │  (shell,   │
    │                                   │               │   command, │
    │                                   │               │   raw)     │
    │                                   │               │  → string  │
    │                                   │               │            │
    │                                   │               │ otherwise: │
    │                                   │               │  → JSON    │
    │                                   │               └────────────┤
    │                                   │                            │
    │                                   │              ansible_runner │
    │                                   │              .run(          │
    │                                   │                module=..., │
    │                                   │                module_args,│
    │                                   │                inventory,  │
    │                                   │                host_pattern│
    │                                   │              )             │
    │                                   │                     │      │
    │                                   │                     │ SSH  │
    │                                   │                     ▼      │
    │                                   │              ┌───────────┐ │
    │                                   │              │ Target    │ │
    │                                   │              │ Host      │ │
    │                                   │              │           │ │
    │                                   │              │ Python    │ │
    │                                   │              │ executes  │ │
    │                                   │              │ module    │ │
    │                                   │              └─────┬─────┘ │
    │                                   │                    │       │
    │                                   │              ◄─────┘       │
    │                                   │                            │
    │                                   │         _parse_runner_result│
    │                                   │               ┌────────────┤
    │                                   │               │ Scan events│
    │                                   │               │ for "res"  │
    │                                   │               │ Extract:   │
    │                                   │               │  status    │
    │                                   │               │  changed   │
    │                                   │               │  result    │
    │                                   │               │  stdout    │
    │                                   │               │  stderr    │
    │                                   │               └────────────┤
    │                                   │                            │
    │  ◄── {status, changed, result, stdout, stderr} ───────────────┤
```

### The Module Args Pipeline (Detail)

This is the most subtle part. The LLM sends a `module_args` dict. Rocannon must serialize it correctly for ansible-runner, which expects a string.

```python
# Input from LLM:
module_args = {"content": "hello", "dest": "/tmp/test"}

# executor.py logic:
args = dict(module_args)
free_form = args.pop("_raw_params", None) or args.pop("cmd", None)

if free_form and not args:
    # Free-form module (command, shell, raw): pass as plain string
    args_str = str(free_form)
    # e.g. "echo hello"
else:
    if free_form:
        args["_raw_params"] = free_form
    # Structured module: pass as JSON
    args_str = json.dumps(args) if args else ""
    # e.g. '{"content": "hello", "dest": "/tmp/test"}'

# ansible-runner receives:
ansible_runner.run(
    module="ansible.builtin.copy",
    module_args='{"content": "hello", "dest": "/tmp/test"}',
    inventory=["/path/to/podman.yml"],
    host_pattern="podman-alpine",
)
```

Free-form detection matters because `ansible.builtin.command` and `ansible.builtin.shell` expect a raw command string, not JSON. Everything else gets JSON-serialized.

## File Map

```
rocannon/
├── src/rocannon/
│   ├── __init__.py           # Package marker
│   ├── cli.py                # Click CLI: --profile, --inventory, --modules, --log-level
│   ├── config.py             # Pydantic Config model + YAML profile loader
│   ├── inventory.py          # Recursive YAML inventory parser + host merger
│   ├── schema.py             # ansible-doc → MCP schema translator
│   ├── server.py             # FastMCP server factory + tool registration
│   └── executor.py           # ansible-runner wrapper + result parser
├── profiles/
│   └── local-dev.yml         # Module specs + inventory paths
├── inventories/
│   ├── local.yml             # localhost (ansible_connection: local)
│   └── podman.yml            # 3 podman containers (SSH, become enabled)
├── tests/
│   ├── conftest.py           # Session-scoped server + client fixtures
│   ├── test_e2e.py           # 14 integration tests against live containers
│   ├── test_every_tool.py    # Exhaustive tool execution test
│   ├── test_ollama_stress.py # LLM-driven stress test scaffold
│   └── zos/                  # z/OS schema validation (gitignored)
│       └── test_schema_zos.py
├── pyproject.toml            # v0.3.0, deps, ruff + pytest config
├── .mcp.json                 # MCP server definition for clients
└── .gitignore
```

## Security Model

One rule: **the host must be in a loaded inventory**.

```python
# server.py — _make_tool_fn()
if host not in valid_hosts:
    return {"status": "rejected", "reason": f"Host '{host}' not found..."}
```

This is checked before any execution. The set of valid hosts is computed once at startup from all loaded inventory files and is immutable for the server's lifetime. There is no way for an LLM to execute a module against a host not explicitly listed in an inventory file.

Ansible's own privilege model (become, become_password) is configured in the inventory, not at runtime. The LLM cannot escalate privileges beyond what the inventory defines.

## What Rocannon Does NOT Do

- **No module-specific logic.** Zero `if module == "copy"` anywhere. Every module flows through the same pipeline.
- **No parameter validation.** Rocannon passes `module_args` straight to ansible-runner. Ansible validates parameters — if you pass garbage, Ansible returns a structured error, and Rocannon returns that error to the LLM.
- **No output interpretation.** The raw `result` dict from Ansible is returned as-is. The LLM interprets what `changed: true` or `stat.exists: false` means.
- **No playbook generation.** Rocannon operates at the ad-hoc module level. Playbook export is an LLM capability, not a Rocannon feature.
- **No state management.** Each tool call is independent. There's no session, no rollback, no transaction. This matches Ansible's own ad-hoc execution model.

## V3 Validation Results

```
ansible.builtin:       71/71   modules registered (100%)
community.general:    582/582  modules registered (100%)
ibm.ibm_zos_core:      27/27   modules registered (100%)
─────────────────────────────────────────────────────────
Total:                680/680  (100% registration rate)

E2E tests:             14/14   passed
z/OS schema tests:      7/7    passed

Rocannon-layer failures: 0
```

Every failure observed during testing was either infrastructure (missing executable on Alpine, no Python, SSH overload) or intentional (ansible.builtin.fail). The schema-to-tool-to-execution pipeline has no known failure modes.
