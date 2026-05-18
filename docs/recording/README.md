# Demo recording

The GIF embedded in the top-level `README.md` shows mcphost driving a local
LLM (Granite 4.1:3b via Ollama) to call typed Ansible-module MCP tools against
a real Red Hat UBI9 container. Recorded with [asciinema](https://asciinema.org),
converted with [agg](https://github.com/asciinema/agg).

## One-time setup

```bash
brew install asciinema agg
docker pull redhat/ubi9-minimal      # or rely on first-run pull during setup
ollama pull granite4.1:3b
```

The model needs to be present in Ollama before recording.

## Regenerate

```bash
# Build the UBI9 SSH container + generate profile/mcp.json under /tmp:
./docs/recording/setup-demo-env.sh

# Record the session and convert:
rm -f docs/assets/demo.cast docs/assets/demo.gif
asciinema rec docs/assets/demo.cast -c "bash docs/recording/demo.sh" \
  --rows 38 --cols 115 --overwrite
agg --theme monokai --speed 1.6 --font-size 16 \
  docs/assets/demo.cast docs/assets/demo.gif

# Optional: tear down the demo container when done
docker rm -f rocannon-demo-ubi9
```

Commit both `demo.cast` (replayable on asciinema.org) and `demo.gif`
(embedded in the README).

## What the demo shows

Splash, then three mcphost invocations back-to-back against the same UBI9
SSH container. Each loads a Rocannon MCP server with a couple of Ansible
modules and gives Granite one prompt:

1. **`ansible.builtin.command` (free -h):** Granite picks the command module,
   runs `free -h`, summarizes total + available memory.
2. **`ansible.builtin.command` (uname -r):** Granite reports the kernel version.
3. **`ansible.builtin.setup`:** Granite runs fact-gathering and pulls the OS
   distribution + version out of the structured result.

Real target. Real tool calls. Real structured output.

## Switching models

```bash
ROCANNON_DEMO_MODEL=ollama:granite4.1:8b-q3_K_M ./docs/recording/demo.sh
ROCANNON_DEMO_MODEL=openai:gpt-4o-mini ./docs/recording/demo.sh
```

The 3B model is the smallest one with reliable tool calling. Larger Granite
variants and most cloud frontier models work, but 8B on CPU can take minutes
per response.

## Why not vhs

We tried vhs 0.11 first. On macOS Tahoe, the shell vhs spawns via ttyd
doesn't echo typed input to the rendered frames, even vhs's own canonical
example produces a blank GIF. asciinema records actual session output
directly with no PTY emulation layer.
