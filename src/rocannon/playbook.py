"""Saved playbooks: YAML files under ``.rocannon/playbooks/`` that record a
sequence of MCP tool calls. Each step is ``{tool, args}``. The legacy Ansible
shape ``{module, target, args}`` is migrated on load (target folds into args).
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

PLAYBOOK_DIR_NAME = ".rocannon/playbooks"
DATA_DIR_ENV = "ROCANNON_DATA_DIR"


def resolve_data_root(root: Path | None = None) -> Path:
    """Pick the directory that holds .rocannon/.

    Precedence: explicit ``root`` argument → ``$ROCANNON_DATA_DIR`` →
    current working directory. The launcher (mcphost config, systemd unit,
    Docker entry, ad-hoc ``rocannon mcp``) is responsible for setting the env
    var when the process CWD wouldn't be the right place to write to.
    """
    if root is not None:
        return root
    env = os.environ.get(DATA_DIR_ENV)
    if env:
        return Path(env)
    return Path.cwd()

# Filesystem-safe slug: letters, digits, dash, underscore. No leading dot or
# dash (avoids hidden files and option-parsing surprises if listed on a CLI).
_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]{0,63}$")


class PlaybookError(ValueError):
    """Raised on validation or persistence failures."""


@dataclass
class PlaybookStep:
    """One step: a tool name plus its arguments.

    The Ansible-specific shape (``module``/``target`` as separate top-level
    fields) is migrated into ``tool``/``args`` on read so legacy YAML keeps
    working. ``target`` becomes ``args["target"]``.
    """

    tool: str
    args: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PlaybookStep:
        # Legacy shape: {module, target, args} → migrate inline.
        if "module" in raw and "tool" not in raw:
            args = dict(raw.get("args", {}) or {})
            if "target" in raw:
                args["target"] = str(raw["target"])
            return cls(tool=str(raw["module"]), args=args)

        if "tool" not in raw:
            raise PlaybookError(f"step missing 'tool': {raw!r}")
        return cls(
            tool=str(raw["tool"]),
            args=dict(raw.get("args", {}) or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Playbook:
    name: str
    description: str
    steps: list[PlaybookStep]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Playbook:
        if "name" not in raw or "steps" not in raw:
            raise PlaybookError("playbook YAML must contain 'name' and 'steps'")
        validate_name(raw["name"])
        steps_raw = raw["steps"]
        if not isinstance(steps_raw, list) or not steps_raw:
            raise PlaybookError("'steps' must be a non-empty list")
        return cls(
            name=raw["name"],
            description=str(raw.get("description", "") or ""),
            steps=[PlaybookStep.from_dict(s) for s in steps_raw],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "steps": [s.to_dict() for s in self.steps],
        }


def validate_name(name: str) -> None:
    """Ensure ``name`` is a safe slug for use as a filename and MCP prompt id."""
    if not _NAME_RE.fullmatch(name or ""):
        raise PlaybookError(
            f"invalid playbook name {name!r}: must match [A-Za-z0-9_][A-Za-z0-9_-]{{0,63}}"
        )


def playbook_dir(root: Path | None = None) -> Path:
    """Return the directory holding saved playbooks. See ``resolve_data_root``."""
    return resolve_data_root(root) / PLAYBOOK_DIR_NAME


def save_playbook(playbook: Playbook, root: Path | None = None, overwrite: bool = False) -> Path:
    """Serialize ``playbook`` to ``<root>/.rocannon/playbooks/<name>.yml``."""
    validate_name(playbook.name)
    target_dir = playbook_dir(root)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{playbook.name}.yml"
    if path.exists() and not overwrite:
        raise PlaybookError(f"playbook {playbook.name!r} already exists at {path}")
    path.write_text(yaml.safe_dump(playbook.to_dict(), sort_keys=False))
    return path


def load_playbook(path: Path) -> Playbook:
    """Load a single playbook YAML."""
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise PlaybookError(f"{path}: invalid YAML, {exc}") from exc
    if not isinstance(raw, dict):
        raise PlaybookError(f"{path}: top-level YAML must be a mapping")
    return Playbook.from_dict(raw)


def load_all_playbooks(root: Path | None = None) -> dict[str, Playbook]:
    """Load every ``*.yml`` under the playbook dir. Bad files are skipped silently."""
    pb_dir = playbook_dir(root)
    out: dict[str, Playbook] = {}
    if not pb_dir.is_dir():
        return out
    for path in sorted(pb_dir.glob("*.yml")):
        try:
            pb = load_playbook(path)
        except PlaybookError:
            # Caller decides whether to log; this function stays silent so it
            # remains usable from constructors and tests.
            continue
        out[pb.name] = pb
    return out


def validate_against_tools(
    playbook: Playbook, tool_names: set[str]
) -> list[str]:
    """Return human-readable problems comparing ``playbook`` to the registered tools.

    Cross-cannon-friendly: only checks that each step's tool name is registered.
    Per-arg validation is left to the runtime (FastMCP's Pydantic layer rejects
    bad args at call time anyway). Empty list = OK.
    """
    problems: list[str] = []
    for idx, step in enumerate(playbook.steps):
        if step.tool not in tool_names:
            problems.append(
                f"step {idx}: tool {step.tool!r} not registered on this server"
            )
    return problems


# Backward-compat alias. Older callers passed a schema_cache; we now only need
# the tool names. Accept either shape, a dict's keys are its tool names.
def validate_against_schemas(
    playbook: Playbook, schema_cache: dict[str, Any]
) -> list[str]:
    return validate_against_tools(playbook, set(schema_cache))
