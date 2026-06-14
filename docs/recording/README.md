# Demo recording

The GIFs in the docs are recorded with [asciinema](https://asciinema.org) and
converted with [agg](https://github.com/asciinema/agg).

- `docs/assets/demo-agent.gif` (headline, in the top-level `README.md`): Claude
  Haiku via the Claude Agent SDK driving Rocannon's typed Ansible-module tools
  against a real RHEL 9 (UBI9) container.
- `docs/assets/demo-ceos.gif` (in `examples/containerlab/`): the same agent
  driving a two-node Arista cEOS fabric under containerlab.

## One-time setup

```bash
brew install asciinema agg
```

The agent demos use the Claude Agent SDK against the logged-in `claude` CLI
session, so no API key is needed. Install `claude-agent-sdk` in the project
environment (`uv pip install claude-agent-sdk`).

## Regenerate the headline (RHEL 9)

```bash
# Build the UBI9 SSH container + generate profiles under /tmp:
bash docs/recording/setup-demo-env.sh

rm -f docs/assets/demo-agent.cast docs/assets/demo-agent.gif
asciinema rec docs/assets/demo-agent.cast -c "bash docs/recording/demo-agent.sh" \
  --rows 34 --cols 100 --overwrite
agg --theme monokai --speed 1.4 --idle-time-limit 1.5 --font-size 16 \
  docs/assets/demo-agent.cast docs/assets/demo-agent.gif

docker rm -f rocannon-demo-ubi9        # teardown
```

## Regenerate the network clip (cEOS)

Needs a running containerlab cEOS lab (see `examples/containerlab/`). Point the
agent at the Rocannon server on the lab host over SSH:

```bash
export ROCANNON_SSH=user@labhost
export ROCANNON_SSH_CMD="cd /path/to/rocannon && uv run rocannon mcp serve --profile /path/to/ceos-profile.yml"

rm -f docs/assets/demo-ceos.cast docs/assets/demo-ceos.gif
asciinema rec docs/assets/demo-ceos.cast -c "bash docs/recording/demo-ceos.sh" \
  --rows 32 --cols 100 --overwrite
agg --theme monokai --speed 1.4 --idle-time-limit 1.5 --font-size 16 \
  docs/assets/demo-ceos.cast docs/assets/demo-ceos.gif
```

Commit both the `.cast` (replayable on asciinema.org) and the `.gif` (embedded
in the docs).

## Why not vhs

We tried vhs 0.11 first. On macOS Tahoe, the shell vhs spawns via ttyd doesn't
echo typed input to the rendered frames; even vhs's own canonical example
produces a blank GIF. asciinema records actual session output directly with no
PTY emulation layer.
