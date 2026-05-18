# AGENTS.md

Instructions for AI coding agents working on this repo (Claude Code,
Cursor, Bob, Codex, etc.). Humans, start with `README.md`.

## What rocannon is

An MCP server that registers every installed Ansible module as a typed MCP
tool. One server, every module auto-discovered from `ansible-doc` at startup.
There is no plugin abstraction: Ansible is the engine. Module discovery and
tool registration live in `src/rocannon/ansible.py`; cross-cutting concerns
(middleware, profile switching, save/replay) live in `src/rocannon/server.py`.

## Setup

```bash
uv sync                     # all dev deps including the `ansible` extra
./tests/check.sh            # ruff format + ruff check + mypy + pytest
```

`uv` is required. Do not use `pip`, `python -m venv`, `pipx`, or
`conda`. If `uv` is missing, install it (`brew install uv` or `curl
-LsSf https://astral.sh/uv/install.sh | sh`) before doing anything
else.

## Quality gates

Every change must pass `./tests/check.sh` before commit. The script
runs four steps and exits on the first failure:

1. `ruff format --check`
2. `ruff check`
3. `mypy --strict` (against `src/` and `tests/`)
4. `pytest -x -q` (unit tests only; integration suite is opt-in)

Auto-fix mode: `./tests/check.sh --fix` (format and lint auto-fix; type
and test errors still surface).

Mypy runs in strict mode. The codebase has targeted `# type:
ignore[<code>]` comments where the static and dynamic type systems
meet (FastMCP dynamic signatures, the rare untyped third-party
import). Do not broaden these to bare `# type: ignore` and do not add
new ignores without a specific error code.

## Architecture

```
src/rocannon/
├── cli.py              Typer entrypoint. Subcommands: mcp serve|doctor,
│                       repl, run, doctor, doc, search, ls, playbook.
├── config.py           Pydantic Config model + YAML profile loader.
│                       Resolves profile-relative paths against the profile
│                       file's parent, not the process CWD.
├── profiles.py         .rocannon/profiles/ discovery, ProfileRegistry,
│                       RuntimeContext for runtime profile switching.
├── ansible.py          register_ansible_modules(): reads ansible-doc,
│                       registers one typed MCP tool per module, plus the
│                       rocannon://inventory and rocannon://module/<fqcn>
│                       resources.
├── server.py           create_server(). Wires middleware, calls
│                       register_ansible_modules, registers save_playbook +
│                       commit_session + rocannon_{list,current,use}_profile.
├── schema.py           ansible-doc parsing, module expansion, type mapping.
├── executor.py         ansible-runner Python-API wrapper + result parsing.
├── playbook.py         Playbook model {tool, args}. Persisted as YAML,
│                       loaded as MCP prompts.
├── repl.py             Operator REPL: same in-process MCP server, prompt
│                       toolkit shell, history, .save, optional .ai mode.
├── inventory.py        ansible-inventory subprocess wrapper.
├── history.py          In-memory ring buffer feeding save_playbook.
├── correlation.py      Request correlation IDs for the audit log.
└── redaction.py        Secrets redaction in audit records.
```

Tests in `tests/` mirror the unit shape. Integration tests
(`tests/test_ansible_integration.py`) carry the `integration` marker
and require docker. They opt-in via `pytest -m integration`; CI does not
run them.

## Adding a non-Ansible execution path

Out of scope. Rocannon is the Ansible MCP server. Other ecosystems
(Salt, Kubernetes, Terraform functions) have different shapes,
different artifact formats, and different MCP server homes. If a
genuine case for another engine arises, it should be a product
decision and likely a separate package, not a plugin slot here.

## Configuration

Profiles are YAML. A profile picks which inventory and modules to
expose. See `examples/quickstart/profile.yml` for the minimal shape;
`examples/profiles/` has one per common scenario. Multiple profiles
under `.rocannon/profiles/` are auto-discovered and can be switched
at runtime via the `rocannon_use_profile` MCP tool.

The MCP transport is `stdio` by default and `http` is supported. The
`rocannon` binary reads `ansible-doc` at startup, so missing binaries
surface in the doctor output, not as obscure runtime errors.

## Testing changes against an MCP client

A working `.mcp.json` ships at the repo root and points at
`examples/quickstart/profile.yml`. After any change to tool
registration or schema parsing:

```bash
claude mcp get rocannon          # health check (expect ✓ Connected)
uv run rocannon mcp doctor --profile examples/quickstart/profile.yml
```

For end-to-end LLM-driven testing, `examples/clients/mcphost.json` +
`mcphost --config <that> --model ollama:granite4.1:3b -p "<prompt>"`
exercises the same registered tools via a real client.

## Conventions

**Voice (applies to code, comments, docs, commit messages).**

- No em-dashes (U+2014). Use a comma, a colon, or a hyphen.
- No AI-pitchy language: leverage, robust, powerful, seamless,
  transform, harness, comprehensive, blazingly, world-class.
- Prefer plain words over decorated ones.

**Code.**

- Default to writing no comments. Only add one when the WHY is
  non-obvious: a hidden constraint, an upstream quirk, a workaround
  whose removal would surprise the reader.
- Don't explain WHAT the code does (well-named identifiers already do
  that) or reference the current task/PR ("used by X", "added for the
  Y flow").
- No half-finished work, no scaffolding for hypothetical future
  requirements, no validation at internal boundaries where the caller
  is trusted.
- Don't add `try/except` that swallows errors. Only catch what you can
  meaningfully handle, and re-raise or annotate the rest.

**Imports.**

- Module-level imports at the top, alphabetised by stdlib / third-party
  / first-party (ruff enforces this).
- No aliased imports unless there is an active name collision.

**Commits.**

- No agent attribution: do not add `Co-Authored-By:`, `Signed-off-by:
  AI`, `Generated with ...` lines, or any other AI attribution.
- Commits are authored by the human user. Use the standard
  `Author:` / `Committer:` git fields.
- One logical change per commit. Avoid mixing unrelated edits.
- Subject in the imperative ("Add LICENSE", not "Added LICENSE").
- Body explains WHY, not WHAT.

**Release.**

- The user authorises every PyPI release. Do not run
  `git tag v*` or `git push --tags` without explicit confirmation;
  `.github/workflows/release.yml` is wired to publish on tag push.

## Common tasks

**Adding a new CLI subcommand.** Use Typer. Put helpers in
`cli.py`'s shared section. Update `README.md`'s "CLI" table.

**Changing tool registration shape.** Run the integration suite
(`uv run pytest -m integration -v`); these tests spin up a real UBI9
SSH container and verify tool callability end-to-end. Then
`claude mcp get rocannon` against the project-level `.mcp.json` to
confirm the wire format still parses.

**Touching anything user-visible.** Update `README.md`. If you change
example profile names or paths, update `examples/README.md` and
`examples/clients/README.md` too.

## Before claiming a task is done

- `./tests/check.sh` passes locally.
- New behaviour has a test (unit if possible, integration if it
  touches real infra).
- Any user-visible change is reflected in `README.md`.
- No em-dashes (U+2014) anywhere in the diff. Check with
  `grep -nP '\x{2014}' <files>` or `rg '\u{2014}' <files>`.
- The commit message follows the conventions above.

If the task involves recording a new demo gif, see
`docs/recording/README.md` for the asciinema + agg pipeline (vhs on
macOS Tahoe produces blank GIFs, do not retry that path).
