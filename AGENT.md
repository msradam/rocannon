# Agent Instructions — Continuing Rocannon Development

You are picking up development of Rocannon, an MCP server that exposes Ansible modules as tools. Read `ARCHITECTURE.md` for the full design. This document tells you how to work on it.

## Environment Setup

```bash
# Install all dependencies (runtime + dev)
uv sync

# Verify Ansible is available
uv run ansible --version

# For z/OS testing, install the IBM collection
uv run ansible-galaxy collection install ibm.ibm_zos_core
```

## Running the Server

```bash
# Against z/OS LPARs (CSRT lab)
uv run rocannon serve --profile profiles/zos.yml

# Against local test containers
uv run rocannon serve --profile profiles/local-dev.yml

# With debug logging
uv run rocannon serve --profile profiles/zos.yml --log-level debug
```

To use Rocannon as an MCP server from an MCP client, configure `.mcp.json`:

```json
{
  "mcpServers": {
    "rocannon": {
      "command": "uv",
      "args": [
        "run", "--directory", "/path/to/rocannon",
        "rocannon", "serve", "--profile", "profiles/zos.yml"
      ]
    }
  }
}
```

## Quality Gates

Always run before committing:

```bash
tests/check.sh
```

This runs ruff format, ruff lint, mypy strict, vulture, and pytest — all five must pass. Fix issues before committing — do not weaken the gates.

If you modify source files in `src/rocannon/`, mypy strict mode applies. The codebase has deliberate `# type: ignore` comments in `server.py` at the boundary between static and dynamic typing — do not remove these. If you add new dynamic type construction, add targeted ignore comments with specific error codes (e.g., `# type: ignore[valid-type]`), never broad `# type: ignore`.

## Testing

### Local container tests (LinuxONE distros)

Requires podman or docker. Tests manage their own container lifecycle.

```bash
# Run all LLM integration tests (requires Ollama + granite4:micro)
uv run pytest tests/test_llm.py -v -k linuxone

# Interactive REPL for manual testing
uv run python tests/interactive.py
```

The interactive REPL auto-starts Ollama, pulls the model, and starts test containers. Just type natural language commands.

### z/OS schema tests (no connectivity needed)

```bash
uv run pytest tests/test_llm.py -v -k zos
```

These verify the LLM selects the correct z/OS tools and parameters. They do NOT execute against real z/OS — they only check the model's first tool call. Requires `csrt.yml` inventory to be present and `ibm.ibm_zos_core` collection installed.

### z/OS live testing

This is the next milestone. The schema tests prove tool selection works; live tests prove execution works.

To test against real z/OS LPARs:

1. Ensure SSH connectivity: `ssh rahman@cb8a.pok.stglabs.ibm.com`
2. Verify Ansible can reach the host: `uv run ansible -i csrt.yml cb8a -m ping`
3. Start the server: `uv run rocannon serve --profile profiles/zos.yml`
4. Connect an MCP client and issue tool calls

Key things to validate on z/OS:
- `environment_vars` resolution — ZOAU, PYZ, EBCDIC encoding vars must propagate
- `ibm.ibm_zos_core.zos_ping` — basic connectivity
- `ibm.ibm_zos_core.zos_data_set` — create/delete a sequential dataset
- `ibm.ibm_zos_core.zos_job_submit` — submit JCL and retrieve output
- `ibm.ibm_zos_core.zos_copy` — USS file to dataset copy (encoding conversion)
- Multi-host execution — run against `source_system` group

## Code Conventions

- **No narrating comments.** Don't add comments that restate what the code does. Comments should explain *why*, not *what*. If the code is clear, it needs no comment.
- **Minimal changes.** Don't refactor surrounding code when fixing a bug. Don't add features that weren't requested.
- **`uv` only.** Never use `pip`, `python -m venv`, or `conda`. All commands go through `uv run`.
- **Subprocess for Ansible CLI, in-process for ansible-runner.** This is a deliberate architecture decision — see ARCHITECTURE.md.
- **Test with local models.** The LLM test suite uses Ollama with `ibm/granite4:micro`. No cloud APIs. This is intentional for air-gapped environments.
- **You may run CLI tools.** You are permitted to run `podman`, `ollama`, `ansible`, `ssh`, and other system tools directly. The test infrastructure depends on them.
- **Never author commits as the agent.** Do not add `Co-Authored-By`, `Signed-off-by`, or any other attribution to an AI agent in commit messages. Commits are authored by the user.

## File Map

```
src/rocannon/
├── cli.py          # Click entrypoint, --profile or --inventory/--modules
├── config.py       # Pydantic Config model + YAML profile loader
├── schema.py       # ansible-doc parsing, module expansion, type mapping
├── inventory.py    # ansible-inventory subprocess wrapper
├── server.py       # FastMCP tool registration with dynamic signatures
└── executor.py     # ansible-runner execution + result parsing

profiles/
├── zos.yml         # z/OS full (builtin + ibm_zos_core against CSRT LPARs)
├── zos-demo.yml    # z/OS demo (ibm_zos_core only)
└── local-dev.yml   # Local dev (builtin against localhost + containers)

inventories/
├── podman.yml      # Test containers (linuxone-rhel/sles/ubuntu on localhost)
├── local.yml       # localhost with local connection
├── vsi.yml         # Wazi as a Service cloud z/OS
└── host_vars/
    └── vsi01.yml   # Per-host vars for cloud z/OS instance

csrt.yml            # CSRT lab z/OS LPARs (cb8a-cb89)

tests/
├── conftest.py     # Container lifecycle fixtures (build → start → inventory → teardown)
├── test_llm.py     # LLM integration tests (LinuxONE live + z/OS schema)
├── interactive.py  # Manual REPL with Ollama
└── containers/
    ├── Containerfile.rhel    # RHEL 10 (UBI10 minimal)
    ├── Containerfile.sles    # SLES 16 (BCI 16.0)
    └── Containerfile.ubuntu  # Ubuntu Server 24.04
```

## z/OS Collection Compatibility Matrix

The `ibm.ibm_zos_core` collection has strict version dependencies. Before testing against z/OS, confirm that the versions on your controller and managed nodes line up.

| Collection | ansible-core | ZOAU | z/OS | Python (controller) | Python (z/OS) |
|---|---|---|---|---|---|
| 1.16.x | >=2.16 | >=1.3.6, <1.4.0 | V2R5–V3Rx | 3.10–3.12 | 3.11–3.12 |
| 1.15.x | >=2.15 | >=1.3.5, <1.4.0 | V2R5–V3Rx | 3.9–3.12 | 3.11–3.12 |
| 1.14.x | >=2.15 | >=1.3.4, <1.4.0 | V2R5–V3Rx | 3.9–3.12 | 3.11–3.12 |
| 1.13.x | >=2.15 | >=1.3.3, <1.4.0 | V2R5–V3Rx | 3.9–3.12 | 3.11–3.12 |
| 1.12.x | >=2.15 | >=1.3.2, <1.4.0 | V2R5–V3Rx | 3.9–3.12 | 3.10–3.12 |
| 1.11.x | >=2.15 | >=1.3.1, <1.4.0 | V2R4–V2Rx | 3.9–3.12 | 3.10–3.12 |
| 1.10.x | >=2.15 | >=1.3.0, <1.4.0 | V2R4–V2Rx | 3.9–3.12 | 3.10–3.12 |

Python on z/OS must be **IBM Open Enterprise SDK for Python** — not stock CPython. Controller Python versions follow the ansible-core support matrix.

### If versions don't match

You have two options:

1. **Downgrade the collection on the controller.** Install an older version that matches the ZOAU/Python on your z/OS system:
   ```bash
   uv run ansible-galaxy collection install ibm.ibm_zos_core:==1.13.0 --force
   ```

2. **Point the inventory at different ZOAU/Python paths.** If the z/OS system has multiple ZOAU or Python installations, specify them in inventory `host_vars`:
   ```yaml
   ansible_python_interpreter: /allpython/3.12/usr/lpp/IBM/cyp/v3r12/pyz/bin/python3
   environment_vars:
     PYZ: /allpython/3.12/usr/lpp/IBM/cyp/v3r12/pyz
     ZOAU: /usr/lpp/IBM/zoau/v1.3.6
   ```

After either change, restart the Rocannon server — it reads `ansible-doc` at startup, so it will pick up the new collection version's module schemas automatically.

## Next Steps

Priority order for z/OS validation:

1. **Verify basic connectivity** — `ansible -i csrt.yml cb8a -m ping` from the work machine
2. **Start the server with z/OS profile** — `uv run rocannon serve --profile profiles/zos.yml --log-level debug`
3. **Test via MCP client** — ping, then command (`uname -a`, `cat /etc/os-release`), then z/OS-specific modules
4. **Write live z/OS tests** — mirror `TestLinuxOneLive` but targeting `source_system` group with z/OS modules. Start with `zos_ping`, then `zos_data_set` create/delete, then `zos_job_submit`
5. **Multi-LPAR execution** — test running modules against the full `source_system` group and verify per-host result aggregation
6. **Error handling for z/OS specifics** — EBCDIC encoding issues, dataset allocation failures, JCL errors — make sure these surface clearly in tool results
