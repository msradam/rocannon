# Execution Environment: Rocannon in a frozen Ansible image

An Ansible Execution Environment (EE) is a container image bundling ansible-core,
collections, and their dependencies. It is how production Ansible (and AAP) ships
a known, reproducible environment. Baking Rocannon into one gives a deterministic
MCP tool surface: the same image reflects the same tools on any machine.

This example builds such an image with `ansible-builder` and drives it from
natural language with Claude Haiku.

## Build

```bash
ansible-builder build -t rocannon-ee:demo -f execution-environment.yml
```

The image bakes in (verified): **rocannon 0.1.0, ansible-core 2.21.0,
ansible.posix 2.2.0**.

Base note: Rocannon needs Python 3.12+, so [`execution-environment.yml`](execution-environment.yml)
builds on a `python:3.12` base and installs ansible-core/ansible-runner itself.
For an org EE base that is already Python 3.12+ (an AAP `ee-minimal` image, say),
swap `base_image` and drop the python-symlink build steps.

## A frozen, deterministic tool surface

Running `rocannon mcp doctor` inside the image reflects exactly the bundled
collections:

```
$ docker run --rm -v "$PWD":/cfg rocannon-ee:demo \
    rocannon mcp doctor --profile /cfg/profile.yml
[ ok ] tools:              90
[ ok ] resources:          5
```

Every machine that runs this image sees the same 90 tools, because the
collection set is frozen in the image.

## Natural language into the EE, with Haiku

The MCP server is the EE container itself: the Claude Agent SDK launches
`docker run -i rocannon-ee:demo rocannon mcp serve ...`, so Haiku talks to the
Rocannon baked into the image and executes against the EE's host.
[`agent_demo.py`](agent_demo.py), lightly trimmed from a real run:

```
USER: Run 'uname -a' on the local host and report the kernel version.
  haiku -> mcp__rocannon__ansible_builtin_command  {"target": "localhost", "cmd": "uname -a"}
  haiku: kernel 6.8.0-100-generic, aarch64.

USER: Gather the host facts and tell me the OS distribution and Python version.
  haiku -> mcp__rocannon__ansible_builtin_setup  {"target": "localhost"}
  haiku: Debian 13.5 ("Trixie"), Python 3.12.13, running in a Docker container.
```

Same model, same tools, same Ansible, now shipped as one reproducible image.

## Reproduce

Needs docker, `ansible-builder` (`uv pip install ansible-builder`),
`claude-agent-sdk`, and a logged-in `claude` CLI.

```bash
ansible-builder build -t rocannon-ee:demo -f execution-environment.yml
docker run --rm -v "$PWD":/cfg rocannon-ee:demo \
    rocannon mcp doctor --profile /cfg/profile.yml
uv run python agent_demo.py
docker rmi rocannon-ee:demo        # teardown
```
