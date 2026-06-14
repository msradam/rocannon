# Case study: rocannon in a real Ansible environment

The question this answers: *will rocannon work against my actual Ansible setup,
with my collections and my hosts?*

rocannon adds no runtime of its own. It reads your installed collections with
`ansible-doc`, exposes each module as a typed tool, and executes through
`ansible-runner` over SSH. What comes out is plain Ansible, so a recorded
session replays under `ansible-playbook` with rocannon out of the loop.

Below is a worked example against a real Red Hat UBI9 (RHEL 9.8) node over SSH,
using the stock `ansible.builtin` and `ansible.posix` collections. Every command
and result is from a real run and is reproducible (see [Reproduce](#reproduce)).

## 1. Your collections become typed tools

The profile lists two collections and nothing else:

```yaml
inventories:
  - hosts.ini
modules:
  - ansible.builtin
  - ansible.posix
```

```
$ rocannon mcp doctor --profile profile-casestudy.yml
[ ok ] create_server (active profile: profile-casestudy, available: profile-casestudy)
[ ok ] tools:              90
[ ok ] resources:          5
```

All 90 modules from those two collections became tools. There is no per-module
code in rocannon; the schemas come from `ansible-doc`.

## 2. It connects to a real host

```
$ rocannon ansible.builtin.ping --target ubi9 --inventory hosts.ini
{"status": "successful", "changed": false,
 "result": {"ping": "pong", "ansible_facts": {"discovered_interpreter_python": "/usr/bin/python3.9"}}}
```

## 3. It gathers real facts

```
$ rocannon ansible.builtin.setup --target ubi9 --inventory hosts.ini
# (key facts from the result)
ansible_distribution=RedHat  ansible_distribution_version=9.8
ansible_kernel=6.8.0-100-generic  ansible_pkg_mgr=dnf  ansible_architecture=aarch64
```

## 4. State management is real and idempotent

```
$ rocannon ansible.builtin.copy --target ubi9 --inventory hosts.ini --content 'Managed by rocannon' --dest /etc/motd
status=successful changed=True
$ # run the identical call again
status=successful changed=False
```

## 5. Dry-run before you apply

Modules that support check mode get a `--check` flag (and `--diff`), gated by the
module's declared support. The preview reports what would change and applies
nothing:

```
$ rocannon ansible.builtin.lineinfile --target ubi9 ... --create --check
  preview: status=successful changed=True check_mode=True
  after preview -> file exists: False        # nothing was written
$ # same call without --check
  apply:   status=successful changed=True
  after apply  -> file exists: True
```

## 6. Your other collections work the same way

`ansible.posix` is not special-cased anywhere. It is just another collection on
the path:

```
$ rocannon ansible.posix.authorized_key --target ubi9 --inventory hosts.ini --user root --key '<pubkey>'
status=successful changed=True
```

## 7. No lock-in: a session is standard Ansible

Append `--record runbook.yml` to any call and rocannon writes each one as a play
in a real playbook:

```yaml
# Rocannon session: runbook
- name: ansible.builtin.copy on ubi9
  hosts: ubi9
  gather_facts: false
  tasks:
  - name: ansible.builtin.copy
    ansible.builtin.copy:
      content: 'Managed by rocannon'
      dest: /etc/motd
      ...
- name: ansible.builtin.lineinfile on ubi9
  hosts: ubi9
  gather_facts: false
  tasks:
  - name: ansible.builtin.lineinfile
    ansible.builtin.lineinfile:
      path: /etc/rocannon-demo.conf
      line: feature.enabled=1
      create: true
      ...
```

Run it with stock `ansible-playbook`, no rocannon involved:

```
$ ansible-playbook -i hosts.ini runbook.yml
PLAY RECAP
ubi9                       : ok=2    changed=0    unreachable=0    failed=0
```

`changed=0` because the state already converged in the steps above. The point is
that the artifact is ordinary Ansible your team can read, version, and run
anywhere Ansible runs.

## What this means for your environment

- Any collection you `ansible-galaxy install` shows up as typed tools at startup.
  No adapter, no code generation step you maintain.
- Execution is your Ansible: `ansible-core` plus `ansible-runner`, over your SSH
  and your inventory. rocannon is a thin layer on top.
- Destructive-capable modules carry MCP safety hints, and check/diff give you a
  preview before applying.
- Sessions are standard playbooks, so there is no migration cost and no lock-in.

To freeze a known collection set for production, point rocannon at an Execution
Environment image (built with `ansible-builder`); the tool surface becomes
deterministic across machines. The reflection and execution shown here are
identical, the only difference is where the collections come from.

## Reproduce

Needs docker, the `ansible` extra, and the posix collection:

```bash
ansible-galaxy collection install ansible.posix
bash examples/case-study/run.sh
docker rm -f rocannon-demo-ubi9      # teardown
```
