# Execution environment dispatch: Rocannon on the control host, Ansible in the image

The sibling [`../execution-environment/`](../execution-environment/) example
bakes Rocannon *into* an Ansible Execution Environment and runs the whole MCP
server there. This example is the opposite shape: Rocannon stays on your
control host (wherever `rocannon mcp serve` runs) and dispatches each module
call into a separate, plain EE image, the same pattern AWX and
`ansible-navigator` use. The image here has no Rocannon in it at all, just
`ansible-core` and a couple of collections.

Set this up with `execution_environment` in a profile:

```yaml
execution_environment: rocannon-plain-ee:demo
execution_environment_engine: docker   # or podman
```

Both **discovery** (the `ansible-doc` reflection that builds the tool
surface at startup) and **execution** dispatch into the image. That matters
because Rocannon's whole architecture is `ansible-doc`-driven: if only
execution were containerized, startup would still shell out to a local
`ansible-doc`, which can't see collections that only live in the image.

## Build

```bash
uv tool install ansible-builder   # or: pip install --user ansible-builder
ansible-builder build -t rocannon-plain-ee:demo -f execution-environment.yml
```

The image bakes in (verified): **ansible-core 2.21.1, ansible-runner 2.4.3,
ansible.posix 2.2.1**. No Rocannon Python package at all.

## Run

```bash
rocannon doctor     --profile profile.yml   # checks the docker/podman binary is on PATH
rocannon mcp doctor --profile profile.yml   # tool count, reflected through the image
```

```
$ rocannon doctor --profile profile.yml
...
[ ok ]  ExecutionEnv  image=rocannon-plain-ee:demo docker: /opt/homebrew/bin/docker (Docker version 29.5.0, build 98f1464960)
...

$ rocannon mcp doctor --profile profile.yml
[ ok ] create_server (active profile: profile, available: profile)
[ ok ] tools:              21
[ ok ] resources:          5
```

Then either drive it as an MCP server (`rocannon mcp serve --profile
profile.yml`), or exercise a single module ad hoc, which also dispatches
into the image:

```bash
rocannon ansible.builtin.command --profile profile.yml --target localhost \
    --cmd "cat /etc/os-release" --pretty
```

Verified output, `stdout` trimmed to the interesting lines:

```
PRETTY_NAME="Debian GNU/Linux 13 (trixie)"
```

That's the image's OS, not the host machine's, proof the command actually
ran inside the container.

## Notes

- `hosts`/`profile.yml` target `localhost` via `ansible_connection=local`, so
  the container both dispatches *and* runs against itself, no separate
  managed node or SSH setup needed. Point `inventories:` at a real inventory
  to manage other hosts the same way; the container reaches them exactly
  like a normal Ansible control node would (SSH, a Docker network, whatever
  your inventory specifies).
- If you're on macOS without Docker Desktop (i.e. using Colima), Colima only
  shares specific host paths into its VM. `ansible-runner`'s scratch
  directory (`TMPDIR`-controlled) needs to land under a shared path, or a
  containerized run fails with something like "the playbook could not be
  found" despite the file genuinely existing on the host. Set `TMPDIR` to a
  path under your home directory before running rocannon.
- Swap `base_image` in `execution-environment.yml` for your org's own EE
  base (an AAP `ee-minimal` image, say) and drop the `prepend_*` symlink
  steps if it's already Python 3.12+.

## Teardown

```bash
docker rmi rocannon-plain-ee:demo
```
