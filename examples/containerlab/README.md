# Containerlab: Rocannon driving a network fabric

[Containerlab](https://containerlab.dev) is how network engineers build and test
topologies: real network operating systems in containers, wired together. This
example stands up a two-node [Arista cEOS](https://www.arista.com) fabric and
drives it from natural language with Claude Haiku, through Rocannon's MCP tools.

The tools here are the `arista.eos` modules: `eos_facts`, `eos_command`,
`eos_config`, `eos_banner`. Rocannon reflects them from `ansible-doc` the same
way it reflects `ansible.builtin`, so the network device's modules become typed
MCP tools with no extra wiring.

![cEOS demo](https://raw.githubusercontent.com/msradam/rocannon/main/docs/assets/demo-ceos.gif)

## The lab

[`ceos.clab.yml`](ceos.clab.yml) is two cEOS nodes connected back to back:

```yaml
name: rocannon
topology:
  nodes:
    ceos1: { kind: arista_ceos, image: ceos:4.36.0.1F }
    ceos2: { kind: arista_ceos, image: ceos:4.36.0.1F }
  links:
    - endpoints: ["ceos1:eth1", "ceos2:eth1"]
```

cEOS is not on a public registry. Download `cEOS-lab-<version>.tar.xz` from your
Arista account, then import it (the tag must match `ceos.clab.yml`):

```bash
docker import cEOS-lab-4.36.0.1F.tar.xz ceos:4.36.0.1F   # podman: prefix localhost/
containerlab deploy -t ceos.clab.yml
```

Containerlab boots EOS, enables eAPI, provisions the `admin` user, and adds
`clab-rocannon-ceos1/2` to `/etc/hosts`. The inventory
([`ceos-inventory.yml`](ceos-inventory.yml)) reaches the nodes over eAPI
(`ansible.netcommon.httpapi`) in enable mode.

## The tool surface

```
$ rocannon mcp doctor --profile ceos-profile.yml
[ ok ] tools:              9
[ ok ] resources:          5
```

Four `arista.eos` modules plus Rocannon's own profile/session tools. Each one is
typed from `ansible-doc`: `eos_command` takes a list of commands,
`eos_banner` takes a banner type and text, and so on.

## Natural language into the fabric, with Haiku

[`agent_demo.py`](agent_demo.py) gives Haiku three plain-English tasks and the
Rocannon MCP server. A real run:

```
USER: What model and EOS software version is ceos1 running?
  -> calls arista_eos_eos_facts  {"target": "ceos1"}
  haiku: Model cEOSLab, EOS 4.36.0.1F. Managed over eAPI, running Python 3.13.1.

USER: What is ceos1 directly connected to? Look at its LLDP neighbors.
  -> calls arista_eos_eos_command  {"target": "ceos1", "commands": ["show lldp neighbors"]}
  haiku: ceos1 is connected to ceos2, Ethernet1 to Ethernet1 (TTL 120).

USER: Set the login banner on ceos1 to exactly 'Managed by Rocannon', and tell me whether it changed anything.
  -> calls arista_eos_eos_banner  {"target": "ceos1", "banner": "login", "text": "Managed by Rocannon", "state": "present"}
     result: {"status":"successful","changed":true,"result":{"commands":["banner login","Managed by Rocannon","EOF"]}}
  haiku: Changed: yes. Applied `banner login` / `Managed by Rocannon` / `EOF` on ceos1.
```

Same model, same MCP protocol as the Linux case studies. The only difference is
the modules behind the tools, which Rocannon picks up from whatever collections
are installed.

## Reproduce

Containerlab runs on Linux with docker or podman. Needs the cEOS image (above),
the Arista collections (`ansible-galaxy collection install arista.eos
ansible.netcommon`), `claude-agent-sdk`, and a logged-in `claude` CLI.

```bash
containerlab deploy -t ceos.clab.yml
rocannon mcp doctor --profile ceos-profile.yml
uv run python agent_demo.py
containerlab destroy -t ceos.clab.yml    # teardown
```

If the lab runs on a different host than the agent (for example, containerlab on
a Linux box and the agent on a laptop), set `ROCANNON_SSH=user@labhost` and
`agent_demo.py` will launch Rocannon there over SSH.
