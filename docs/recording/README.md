# Demo recording

The GIF embedded in the top-level `README.md` is generated from `demo.sh` using
[asciinema](https://asciinema.org) for capture and [agg](https://github.com/asciinema/agg)
for GIF conversion.

## Regenerate

From the repo root:

```bash
rm -f docs/assets/demo.cast docs/assets/demo.gif
asciinema rec docs/assets/demo.cast -c "bash docs/recording/demo.sh" \
  --rows 40 --cols 115 --overwrite
agg --theme monokai --speed 1.5 --font-size 18 \
  docs/assets/demo.cast docs/assets/demo.gif
```

Commit both `demo.cast` (replayable on asciinema.org) and `demo.gif`
(embedded in the README).

## Prerequisites

```bash
brew install asciinema agg
```

The `demo.sh` script also needs:

- `uv` (the project itself runs via `uv run`)
- `tofu` on PATH (the quickstart profile loads the Terraform cannon at startup)
- `ansible-doc` on PATH (the Ansible cannon's startup reflection)

## Why asciinema and not vhs

We tried vhs first. vhs 0.11 on macOS Tahoe spawns a shell that doesn't echo
typed input to the rendered frames (a ttyd/chromium compatibility issue at
that combination of versions). Even vhs's own canonical example produced a
blank GIF. asciinema records the actual session output directly, no PTY
emulation, no chromium.

## Tweaking the script

`demo.sh` runs a sequence of `rocannon` CLI calls with `sleep` between them so
each result is readable in the animation. To change what's shown:

- The opening title card is the ANSI-cyan splash followed by a one-line
  tagline. The splash is the same string baked into the REPL's boot output.
- Each `step "..."` line prints a fake prompt + command, then the next line
  runs the actual command. Add or remove steps to change pace.
- `agg`'s `--speed 1.5` plays back 1.5x faster than realtime; tune to taste.
