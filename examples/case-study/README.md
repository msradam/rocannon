# Case study: natural language to ad-hoc Ansible

The question this answers: *can a cheap LLM drive my real Ansible environment
from plain English, and is what it does actually Ansible (not a black box)?*

Setup, all real and reproducible (see [Reproduce](#reproduce)):

- A Red Hat UBI9 (**RHEL 9.8**) node over SSH, a stand-in for a host in your
  inventory.
- The stock `ansible.builtin` and `ansible.posix` collections.
- **Rocannon** exposing those collections as typed MCP tools.
- **Claude Haiku** (a small, cheap model) driving the tools through the
  [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python), using a
  logged-in Claude Code session, no API key. The driver is
  [`agent_demo.py`](agent_demo.py), about 60 lines.

Haiku is given plain-English tasks. It never sees Ansible module names or
arguments in the prompts; it picks the right module from Rocannon's tool surface
and answers.

## The session

Lightly trimmed from a real run (`agent_demo.py` reproduces it):

```
USER: What OS and version is host ubi9 running?
  haiku -> mcp__rocannon__ansible_builtin_setup  {"target": "ubi9"}
  haiku: Red Hat Enterprise Linux 9.8 ("Plow"), kernel 6.8.0, aarch64,
         Python 3.9, package manager dnf.

USER: Run 'uptime' on host ubi9 and report the load averages.
  haiku -> mcp__rocannon__ansible_builtin_command  {"target": "ubi9", "cmd": "uptime"}
  haiku: load average 1.04 (1m), 0.44 (5m), 0.32 (15m); up 1 day, 6h.

USER: Set the message of the day on host ubi9 to exactly 'Managed by Rocannon',
      and tell me whether it changed anything.
  haiku -> mcp__rocannon__ansible_builtin_copy  {"target": "ubi9", "dest": "/etc/motd", "content": "Managed by Rocannon"}
  haiku: Yes, it changed. /etc/motd now contains "Managed by Rocannon"
         (changed=true, 19 bytes, root:root, 0644).
```

Three intents, three Ansible modules, picked by the model: gather facts, run an
ad-hoc command, manage a file. A small model is enough because the tools are
typed and discoverable, the model maps intent to module and Rocannon handles the
schema and execution.

## It is real Ansible, not a black box

Two things keep this honest.

**Your collections, reflected.** The profile lists collections, not modules;
Rocannon reads them with `ansible-doc` and exposes every module:

```
$ rocannon mcp doctor --profile profile-casestudy.yml   # ansible.builtin + ansible.posix
[ ok ] tools:              90
```

**No lock-in.** Append `--record runbook.yml` and the session is written as a
standard playbook that runs with stock `ansible-playbook`, Rocannon out of the
loop:

```
$ ansible-playbook -i hosts.ini runbook.yml
ubi9                       : ok=2    changed=0    unreachable=0    failed=0
```

So the LLM-driven path and a plain `ansible-playbook` are the same Ansible
underneath. You can hand the recorded playbook to a teammate who has never heard
of Rocannon.

## Honest notes

- **Small-model tool selection is not perfect.** Haiku reliably mapped "what
  OS", "run uptime", and "set the motd". It missed "is the host reachable" (it
  read that as a network ping, not `ansible.builtin.ping`). Scope the profile to
  the modules a task needs; fewer, well-named tools select better. Larger models
  miss less.
- **No model required for scripting.** The same execution path is a CLI:
  `rocannon ansible.builtin.command --target ubi9 --cmd uptime`, with
  `--check`/`--diff` for dry-runs and `--record` for the playbook. The LLM is
  one consumer of the tool surface, not a dependency.

## Reproduce

Needs docker, the `ansible` extra, the posix collection, and a logged-in
`claude` CLI (the Agent SDK reuses that session). Run it from a normal shell,
not from inside an active Claude Code session, so the demo's own MCP server is
the one Haiku uses (a nested session inherits the parent's servers instead):

```bash
ansible-galaxy collection install ansible.posix
uv pip install claude-agent-sdk
bash examples/case-study/run.sh
docker rm -f rocannon-demo-ubi9      # teardown
```
