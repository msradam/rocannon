# Rocannon Architecture

Rocannon turns every installed Ansible module into an MCP tool. No module-specific code — it reads `ansible-doc` schemas at startup and dynamically generates typed tool functions that FastMCP exposes over stdio or HTTP.

## Pipeline

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐     ┌──────────────┐
│  CLI / MCP   │────▶│  Schema      │────▶│  Server      │────▶│  Executor    │
│  Client      │     │  Discovery   │     │  Registration│     │  (ansible-   │
│              │◀────│              │◀────│              │◀────│   runner)    │
└─────────────┘     └──────────────┘     └─────────────┘     └──────────────┘
     ▲                     │                    │                     │
     │                     ▼                    ▼                     ▼
     │              ansible-doc -j        ansible-inventory     ansible-runner
     │              (subprocess)          (subprocess)          (Python API)
     │
  FastMCP transport (stdio/http)
```

## Source Files

### `src/rocannon/cli.py` (entrypoint)

Click CLI with a single `serve` command. Two modes of configuration:

- `--profile profiles/zos.yml` — loads a YAML file with `inventories` and `modules` keys
- `--inventory ... --modules ...` — inline flags (repeatable, mutually exclusive with `--profile`)

Constructs a `Config`, calls `create_server()`, and runs the FastMCP transport. The `rocannon` console_script entry point in `pyproject.toml` maps to `cli:main`.

### `src/rocannon/config.py` (configuration)

Pydantic `BaseModel` with three fields:

| Field         | Type         | Purpose                                         |
|---------------|--------------|--------------------------------------------------|
| `inventories` | `list[Path]` | Ansible inventory files (validated to exist)     |
| `modules`     | `list[str]`  | Module specs — FQCNs, collections, or namespaces |
| `transport`   | `str`        | `"stdio"` or `"http"`                            |

`load_profile(path)` reads a YAML file into a `Config`. Pydantic validators ensure at least one inventory and one module spec are provided, and resolve all paths to absolute.

### `src/rocannon/schema.py` (module discovery)

Two responsibilities:

1. **`expand_modules(specs)`** — Takes a mix of fully-qualified module names (3+ dots, e.g. `ansible.builtin.ping`) and prefix specs (e.g. `ibm.ibm_zos_core`). Runs `ansible-doc --list -j` once, then filters the full module list by prefix. Returns sorted, deduplicated FQCNs.

2. **`fetch_module_schema(module_name)`** — Runs `ansible-doc -t module -j <name>` and parses the JSON output into a normalized dict: `{name, description, parameters: [{name, description, required, type, default, choices, elements}]}`. Sub-options are flattened into the description string. Gracefully returns a stub schema on failure.

`ANSIBLE_TYPE_MAP` maps Ansible type strings (`str`, `int`, `bool`, `list`, `dict`, `path`, `raw`, `jsonarg`, etc.) to Python types for MCP schema generation.

### `src/rocannon/inventory.py` (inventory loading)

Runs `ansible-inventory --list -i <path>` via subprocess and parses the JSON output. Returns `{"hosts": [...], "groups": [...]}`. Groups are filtered to only those that actually contain hosts (excludes `all`, `ungrouped`, and empty groups).

Uses subprocess instead of the Ansible Python API because Ansible's `_collection_finder.py` FileFinder import hook conflicts with FastMCP when loaded in the same process.

### `src/rocannon/server.py` (tool registration)

The core of Rocannon. `create_server(config)` orchestrates everything:

1. Loads inventory → gets hosts and groups
2. Expands module specs → gets list of FQCNs
3. For each module, fetches its schema and calls `_register_tool()`

**`_register_tool()`** creates a dynamically-typed async function for each module:

- **Target parameter**: Built from the inventory. Small inventories (≤30 targets) use `Literal["host1", "host2", ...]` so the LLM sees exact valid values. Larger inventories fall back to a described `str`.
- **Module parameters**: Each Ansible parameter is mapped to a Python type via `_ansible_type_to_python()`. Choices become `Literal` types. Lists get element type info. Required params have no default; optional params default to their Ansible default or `None`.
- **Name sanitization**: Ansible param names can contain characters invalid in Python identifiers (hyphens, dots). `_sanitize_param_name()` converts these and avoids collisions with reserved names (`target`, `ctx`) and Python keywords.
- **Signature construction**: Uses `inspect.Parameter` with `KEYWORD_ONLY` to avoid ordering issues (required-after-optional). The constructed `__signature__` and `__annotations__` are applied to the function so FastMCP generates correct JSON schema.
- **Context injection**: A FastMCP `Context` parameter is appended (invisible to the LLM) for logging.

When called, the tool function:
1. Pops `target` and `ctx` from kwargs
2. Reverse-maps sanitized Python names back to original Ansible param names
3. Strips `None` values
4. Delegates to `run_module()` via `asyncio.to_thread()`

### `src/rocannon/executor.py` (Ansible execution)

`run_module()` builds a single-task playbook on the fly:

```yaml
- hosts: <target>
  gather_facts: false
  environment: "{{ environment_vars | default({}) }}"
  tasks:
    - name: Execute <module>
      <module>: <module_args>
```

The `environment_vars` Jinja2 reference is key for z/OS — the z/OS inventory defines `environment_vars` with EBCDIC encoding settings, ZOAU paths, etc. Ansible resolves this from the inventory at runtime.

Writes the playbook to a tempfile, runs it through `ansible_runner.run()`, and cleans up. `_parse_runner_result()` collects per-host results from runner events. Single-host responses are flattened; multi-host responses return a `hosts` dict.

## Inventory Files

### `inventories/podman.yml`

Local test inventory — three containers (RHEL 10, SLES 16, Ubuntu 24.04) on localhost ports 2222-2224, with password auth. Hostnames: `linuxone-rhel`, `linuxone-sles`, `linuxone-ubuntu`. Group: `linuxone`.

### `csrt.yml`

IBM z/OS LPAR inventory for the CSRT lab. Eight LPARs (`cb8a` through `cb89`) at `*.pok.stglabs.ibm.com`. Defines `environment_vars` for EBCDIC encoding, ZOAU, and Python on z/OS paths. Group: `source_system`.

### `inventories/vsi.yml` + `inventories/host_vars/vsi01.yml`

Wazi as a Service (cloud z/OS) instance. Single host `vsi01` with IBM Python 3.13 and ZOAU paths.

## Profiles

Profiles are YAML files that bundle inventories + modules for a specific use case:

| Profile              | Inventories           | Modules                                  |
|----------------------|-----------------------|------------------------------------------|
| `profiles/zos.yml`   | `csrt.yml`            | `ansible.builtin`, `ibm.ibm_zos_core`   |
| `profiles/zos-demo.yml` | `csrt.yml`         | `ibm.ibm_zos_core`                       |
| `profiles/local-dev.yml` | `local.yml`, `podman.yml` | `ansible.builtin`               |

## Test Infrastructure

### Container test targets (`tests/containers/`)

Three Containerfiles matching IBM LinuxONE supported distros:

| File                  | Base Image                                         | Package Manager | SSH Config Method           |
|-----------------------|----------------------------------------------------|-----------------|-----------------------------|
| `Containerfile.rhel`  | `registry.access.redhat.com/ubi10/ubi-minimal:latest` | microdnf     | `sed` on `sshd_config`     |
| `Containerfile.sles`  | `registry.suse.com/bci/bci-base:16.0`             | zypper          | Drop-in at `sshd_config.d/` |
| `Containerfile.ubuntu`| `ubuntu:24.04`                                     | apt-get         | `sed` on `sshd_config`     |

Each container: installs OpenSSH + Python 3 + sudo, creates a `rocannon` user with password auth, enables root login, and runs `sshd -D`.

SLES 16 ships OpenSSH 10 which uses `sshd_config.d/` drop-in files instead of a monolithic `sshd_config` — the Containerfile writes directly to `sshd_config.d/rocannon.conf`.

### `tests/conftest.py` (pytest fixtures)

Session-scoped fixtures managing the full container lifecycle:

1. `container_runtime` — detects podman or docker
2. `podman_containers` — builds images, starts containers, yields, then tears down
3. `podman_inventory` — generates a dynamic Ansible inventory YAML pointing to the running containers

Container names (`rocannon-rhel`, etc.) are mapped to inventory hostnames (`linuxone-rhel`, etc.) to match the naming convention of the target environment.

### `tests/test_llm.py` (LLM integration tests)

Uses Ollama with `ibm/granite4:micro` (2B parameter model) for air-gapped, fully local testing. Two test classes:

- **`TestLinuxOneLive`** — Live execution against running containers. Tests: ping single host, ping group, OS detection, file lifecycle (create → stat → delete), multi-host command.
- **`TestZosSchema`** — Schema-only validation (no z/OS connectivity required). Verifies the LLM correctly selects z/OS-specific tools (`zos_ping`, `zos_data_set`, `zos_job_submit`, `zos_copy`) over builtin equivalents, and passes the right target host.

### `tests/interactive.py` (REPL)

Interactive agent loop for manual testing. Auto-starts Ollama, pulls the model, starts test containers, then enters a REPL. Includes a system prompt with inventory context (required for small models) and a consecutive-error bailout to prevent infinite loops.

## Quality Gates

`tests/check.sh` runs five gates in sequence:

1. **ruff format** — Double quotes, 100-char line length
2. **ruff lint** — Rules: `E`, `W`, `F`, `I`, `N`, `UP`, `B`, `C4`, `SIM`, `S` (bandit/security), `FURB` (refurb). Security rules (`S603`, `S607`) are per-file-ignored where subprocess calls to Ansible CLI tools are architectural.
3. **mypy** — Strict mode, Python 3.11 target
4. **vulture** — Dead code detection (min confidence 80, `cls` ignored for Pydantic validators)
5. **pytest** — Full test suite

## Key Design Decisions

**Subprocess for Ansible CLI tools**: `ansible-doc`, `ansible-inventory`, and the collection listing all run as subprocesses. Ansible's import system (specifically `_collection_finder.py`) installs a custom `FileFinder` hook that conflicts with FastMCP's module loading. The subprocess boundary isolates this.

**`ansible-runner` for execution**: Unlike the CLI tools, `ansible-runner` works fine in-process and provides structured event output. The playbook-on-the-fly approach (tempfile → run → delete) avoids any persistent state.

**Dynamic signatures with `inspect.Signature`**: FastMCP generates JSON schema from function signatures. Rocannon constructs these dynamically at registration time, which means mypy can't statically verify the types. The `# type: ignore` comments in `server.py` are deliberate — they mark the boundary between static and dynamic typing.

**`environment_vars` in playbook template**: The `environment: "{{ environment_vars | default({}) }}"` pattern lets z/OS inventories define EBCDIC encoding, ZOAU paths, and other environment setup as inventory variables. Ansible resolves these at runtime, so the executor doesn't need z/OS-specific code.

**`Literal` types for small inventories**: When there are ≤30 valid targets, the tool schema uses `Literal["host1", "host2"]` so the LLM sees exact valid values in the JSON schema. This significantly improves tool-call accuracy, especially with smaller models.
