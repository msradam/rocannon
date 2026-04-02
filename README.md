# Rocannon

Ansible modules as MCP tools — automatically.

Rocannon reads `ansible-doc` schemas at startup and registers every installed Ansible module as an MCP tool. No module-specific code. The LLM sees tool definitions with parameter names, types, and descriptions; Rocannon passes `module_args` through to `ansible-runner` and returns structured results.

**653 modules tested across 3 collections (ansible.builtin, community.general, ibm.ibm_zos_core) at 100% registration rate.**

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) for environment management
- Ansible collections you want to expose (ansible.builtin ships with ansible-core)

## Quick Start

```bash
# Clone and install
git clone <repo-url> rocannon
cd rocannon
uv sync

# Start the MCP server
uv run rocannon serve --profile profiles/local-dev.yml
```

## Setup on a New Machine

### 1. Install dependencies

```bash
uv sync
```

### 2. Create an inventory file

Create a YAML inventory for your target hosts. Example for z/OS:

```yaml
# inventories/zos.yml
all:
  hosts:
    lpar1:
      ansible_host: 10.0.0.1
      ansible_port: 22
      ansible_user: IBMUSER
      ansible_ssh_private_key_file: ~/.ssh/zos_key
      ansible_python_interpreter: /usr/lpp/IBM/cyp/v3r12/pyz/bin/python3.12
    lpar2:
      ansible_host: 10.0.0.2
      ansible_port: 22
      ansible_user: IBMUSER
      ansible_ssh_private_key_file: ~/.ssh/zos_key
      ansible_python_interpreter: /usr/lpp/IBM/cyp/v3r12/pyz/bin/python3.12
```

### 3. Install Ansible collections

```bash
uv run ansible-galaxy collection install ibm.ibm_zos_core
# or any other collection
```

### 4. Create a profile

```yaml
# profiles/zos.yml
inventories:
  - ./inventories/zos.yml
modules:
  - ansible.builtin
  - ibm.ibm_zos_core
```

### 5. Test connectivity

```bash
uv run ansible -i inventories/zos.yml all -m ping
```

### 6. Start the server

```bash
# stdio transport (for MCP clients)
uv run rocannon serve --profile profiles/zos.yml

# with debug logging
uv run rocannon serve --profile profiles/zos.yml --log-level debug
```

### 7. Connect an MCP client

**Claude Code / Claude Desktop** — add to `.mcp.json`:

```json
{
  "mcpServers": {
    "rocannon": {
      "command": "uv",
      "args": [
        "run", "--directory", "/path/to/rocannon",
        "rocannon", "serve", "--profile", "/path/to/rocannon/profiles/zos.yml"
      ]
    }
  }
}
```

**mcphost with a local Ollama model** — interactive agent loop from the terminal:

Prerequisites: [Ollama](https://ollama.com) installed and running, model pulled (`ollama pull ibm/granite4:micro`).

```bash
# 1. Install mcphost — pick your platform from:
#    https://github.com/mark3labs/mcphost/releases/latest
#    Example for macOS arm64:
curl -sL https://github.com/mark3labs/mcphost/releases/latest/download/mcphost_Darwin_arm64.tar.gz \
  | tar xz && mv mcphost /usr/local/bin/

# 2. Configure — mcphost reads ~/.mcphost.yml by default
cp .mcphost.yml.example ~/.mcphost.yml
# Edit ~/.mcphost.yml: replace /path/to/rocannon with your actual path
#   and set --profile to the profile you want (e.g. profiles/local-dev.yml)

# 3. Start an interactive session
mcphost -m ollama:ibm/granite4:micro

# Or run a single non-interactive prompt
mcphost -m ollama:ibm/granite4:micro -p "what OS is running on the ubuntu host?"
```

mcphost starts Rocannon as a stdio subprocess, loads all registered tools, and runs a multi-turn agent loop. Granite selects tools, Rocannon executes them via Ansible, results come back as structured JSON. Type natural language — no playbooks needed.

**vLLM or any OpenAI-compatible endpoint** — point mcphost at it with `--provider-url`:

```bash
mcphost -m openai:ibm-granite/granite-3.3-8b-instruct \
  --provider-url http://your-vllm-host:8000/v1 \
  --provider-api-key x
```

The model name must match what vLLM was started with (`--served-model-name`). The API key is required by the client but vLLM ignores it unless you configure auth.

## CLI Reference

```
rocannon serve [OPTIONS]

Options:
  --profile PATH       YAML profile file (inventories + modules)
  --inventory PATH     Inventory file, repeatable (alt to --profile)
  --modules TEXT       Module/collection/namespace, repeatable (alt to --profile)
  --transport TEXT     stdio or http [default: stdio]
  --log-level TEXT     DEBUG, INFO, WARNING, ERROR [default: INFO]
```

`--profile` and `--inventory/--modules` are mutually exclusive. Use profiles for reproducibility.

## How It Works

1. **Startup**: Reads `ansible-doc --list -j` to expand collection specs (e.g. `ibm.ibm_zos_core`) into individual module names
2. **Registration**: For each module, runs `ansible-doc -j <module>` and extracts parameter schemas → registers as MCP tool
3. **Execution**: When a tool is called, validates the host against loaded inventories, serializes `module_args`, and runs via `ansible-runner`
4. **Result**: Parses ansible-runner events and returns `{status, changed, result, stdout, stderr}`

## Security

Hosts are validated against loaded inventories before any execution. If a host isn't in an inventory file, the call is rejected. Privilege escalation (become) is configured in the inventory, not at runtime.

## Architecture

Rocannon has three stages at startup and one at runtime:

1. **Expand** — `ansible-doc --list -j` turns collection/namespace specs (e.g. `ibm.ibm_zos_core`) into individual module FQCNs
2. **Introspect** — `ansible-doc -j <module>` for each FQCN extracts parameter names, types, defaults, choices, and descriptions
3. **Register** — each module becomes a FastMCP tool with a fully typed Python signature; the `target` parameter is constrained to a `Literal` of known hosts/groups
4. **Execute** — tool calls serialize `module_args` into a temporary playbook and run it via `ansible-runner`; results are parsed from runner events and returned as structured JSON
