"""Saved playbooks: standard Ansible YAML under ``.rocannon/playbooks/``.

On-disk shape is a list of plays, one per recorded step; the step's
``target`` becomes the play's ``hosts:``. ``ansible-playbook -i <inv>
<file>`` runs the file directly with no Rocannon in the loop.

In-memory shape is ``Playbook.steps`` (a flat list of
``PlaybookStep(tool, args)``) for replay through the MCP server.
Conversion between the two happens only at the file boundary
(``to_ansible_yaml`` / ``from_ansible_plays``).

Legacy on-disk shape (``{name, description, steps: [{tool, args}]}``)
still loads.
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

    Precedence: explicit ``root`` argument, ``$ROCANNON_DATA_DIR``,
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

# Task-level Ansible keywords that should not be mistaken for a module name
# when parsing a hand-edited playbook back into Rocannon's flat step model.
# Anything else with a dot in it is treated as the module FQCN.
_TASK_CONTROL_KEYS = frozenset(
    {
        "name",
        "when",
        "register",
        "tags",
        "delegate_to",
        "vars",
        "loop",
        "loop_control",
        "with_items",
        "until",
        "retries",
        "delay",
        "ignore_errors",
        "changed_when",
        "failed_when",
        "no_log",
        "become",
        "become_user",
        "become_method",
        "become_flags",
        "environment",
        "notify",
        "listen",
        "block",
        "rescue",
        "always",
        "any_errors_fatal",
        "async",
        "poll",
        "check_mode",
        "diff",
        "connection",
        "port",
        "remote_user",
        "throttle",
        "run_once",
        "module_defaults",
        "args",
        "action",
        "local_action",
    }
)


class PlaybookError(ValueError):
    """Raised on validation or persistence failures."""


@dataclass
class PlaybookStep:
    """One step: a tool name plus its arguments.

    Legacy on-disk shapes are normalized into ``tool``/``args`` on load:
    ``{module, target, args}`` and the modern Ansible-playbook task form both
    map to this internal representation.
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
    def from_legacy_dict(cls, raw: dict[str, Any]) -> Playbook:
        """Load the original Rocannon shape: ``{name, description, steps}``."""
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

    @classmethod
    def from_ansible_plays(cls, name: str, raw_text: str, plays: list[Any]) -> Playbook:
        """Parse a standard Ansible playbook (list of plays) into a Playbook.

        Each play's tasks become steps; the play's ``hosts:`` becomes each
        task's ``target`` arg. The module is whichever task key contains a
        dot and isn't a known task-control keyword.

        ``raw_text`` is the original file content; its leading ``# Rocannon
        session:`` comment and following comment lines (if present) supply
        the description.
        """
        validate_name(name)
        description = _parse_description_header(raw_text)

        steps: list[PlaybookStep] = []
        for play in plays:
            if not isinstance(play, dict):
                continue
            target = str(play.get("hosts", "all"))
            for task in play.get("tasks") or []:
                step = _task_to_step(task, target)
                if step is not None:
                    steps.append(step)

        if not steps:
            raise PlaybookError("playbook has no recognizable Ansible tasks")
        return cls(name=name, description=description, steps=steps)

    def to_ansible_plays(self) -> list[dict[str, Any]]:
        """Render the internal model as a list of Ansible plays."""
        plays: list[dict[str, Any]] = []
        for step in self.steps:
            args = step.args.copy()
            target = str(args.pop("target", "all"))
            task: dict[str, Any] = {"name": step.tool, step.tool: args}
            plays.append(
                {
                    "name": f"{step.tool} on {target}",
                    "hosts": target,
                    "gather_facts": False,
                    "tasks": [task],
                }
            )
        return plays

    def to_ansible_yaml(self) -> str:
        """Render this playbook as standard Ansible YAML with a header comment."""
        plays = self.to_ansible_plays()
        body = yaml.safe_dump(plays, sort_keys=False, default_flow_style=False)
        header = [f"# Rocannon session: {self.name}"]
        if self.description:
            for line in self.description.splitlines():
                header.append(f"# {line}" if line else "#")
        header.append("")
        return "\n".join(header) + "\n" + body


def _parse_description_header(raw_text: str) -> str:
    """Pull the description out of leading ``# `` comments after the marker."""
    lines: list[str] = []
    seen_marker = False
    for raw in raw_text.splitlines():
        line = raw.rstrip()
        if line.startswith("# Rocannon session:"):
            seen_marker = True
            continue
        if not seen_marker:
            if line.startswith("#") or line == "":
                continue
            break
        if line.startswith("# "):
            lines.append(line[2:])
        elif line == "#":
            lines.append("")
        elif line == "":
            continue
        else:
            break
    return "\n".join(lines).strip()


def _task_to_step(task: Any, target: str) -> PlaybookStep | None:
    """Convert one Ansible task dict back to a Rocannon PlaybookStep.

    Returns None when the task has no module key (e.g., comments-only or
    a malformed task). The module key is the first key that contains a dot
    and isn't a known Ansible task-control keyword.
    """
    if not isinstance(task, dict):
        return None
    for key, value in task.items():
        if key in _TASK_CONTROL_KEYS:
            continue
        if "." not in key:
            continue
        args = dict(value) if isinstance(value, dict) else {}
        args["target"] = target
        return PlaybookStep(tool=key, args=args)
    return None


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
    """Write ``playbook`` as standard Ansible YAML.

    Output is ``<root>/.rocannon/playbooks/<name>.yml``, a real Ansible
    playbook a sysadmin can run with ``ansible-playbook -i <inv> <file>``.
    """
    validate_name(playbook.name)
    target_dir = playbook_dir(root)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{playbook.name}.yml"
    if path.exists() and not overwrite:
        raise PlaybookError(f"playbook {playbook.name!r} already exists at {path}")
    path.write_text(playbook.to_ansible_yaml())
    return path


def load_playbook(path: Path) -> Playbook:
    """Load a saved playbook, accepting both the new and legacy shapes."""
    raw_text = path.read_text()
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise PlaybookError(f"{path}: invalid YAML, {exc}") from exc

    if isinstance(data, list):
        return Playbook.from_ansible_plays(path.stem, raw_text, data)
    if isinstance(data, dict):
        return Playbook.from_legacy_dict(data)
    raise PlaybookError(f"{path}: top-level YAML must be a list of plays or a dict")


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
            continue
        out[pb.name] = pb
    return out


def validate_against_tools(playbook: Playbook, tool_names: set[str]) -> list[str]:
    """Return human-readable problems comparing ``playbook`` to the registered tools.

    Only checks that each step's tool name is registered. Per-arg validation
    is left to the runtime (FastMCP's Pydantic layer rejects bad args at
    call time anyway). Empty list = OK.
    """
    problems: list[str] = []
    for idx, step in enumerate(playbook.steps):
        if step.tool not in tool_names:
            problems.append(f"step {idx}: tool {step.tool!r} not registered on this server")
    return problems
