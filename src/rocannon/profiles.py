"""Profile discovery, registry, and runtime context.

A profile is a YAML file declaring inventory + modules + ansible config.
`.rocannon/profiles/<name>.yml` files (or `~/.rocannon/profiles/`) are
discovered at startup.

Default-profile resolution: `default.yml` symlink target, then `default.yml`
as a regular file, then the sole profile if there is exactly one.

`RuntimeContext` holds the active profile name behind an asyncio.Lock.
Tool functions read `active_config()` on every call, so switching the
active profile takes effect without re-registering tools.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from rocannon.config import Config, load_profile

logger = logging.getLogger("rocannon.profiles")


@dataclass(frozen=True)
class LoadedProfile:
    name: str
    path: Path
    config: Config


@dataclass
class ProfileRegistry:
    """Every profile discovered at startup, plus the default-name resolution."""

    profiles: dict[str, LoadedProfile] = field(default_factory=dict)
    default_name: str | None = None
    source_dir: Path | None = None

    def names(self) -> list[str]:
        return sorted(self.profiles)

    def get(self, name: str) -> LoadedProfile:
        if name not in self.profiles:
            raise KeyError(
                f"Unknown profile {name!r}. Available: {', '.join(self.names()) or '(none)'}"
            )
        return self.profiles[name]


def discover_profiles_dir(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default: CWD) looking for `.rocannon/profiles/`.

    Falls back to `~/.rocannon/profiles/` if found. Returns None if neither exists.
    """
    cwd = (start or Path.cwd()).resolve()
    for parent in (cwd, *cwd.parents):
        candidate = parent / ".rocannon" / "profiles"
        if candidate.is_dir():
            return candidate
    user_level = Path.home() / ".rocannon" / "profiles"
    if user_level.is_dir():
        return user_level
    return None


def load_profile_registry(
    profiles_dir: Path,
    transport: str = "stdio",
) -> ProfileRegistry:
    """Load every `*.yml` in `profiles_dir` and resolve the default."""
    if not profiles_dir.is_dir():
        raise FileNotFoundError(f"profiles directory not found: {profiles_dir}")

    registry = ProfileRegistry(source_dir=profiles_dir.resolve())
    default_path = profiles_dir / "default.yml"
    default_target_resolved: Path | None = None
    if default_path.is_symlink():
        default_target_resolved = default_path.resolve()

    for path in sorted(profiles_dir.glob("*.yml")):
        if path.name == "default.yml" and path.is_symlink():
            # The symlink target is loaded under its own name; skip the alias.
            continue
        name = path.stem
        try:
            cfg = load_profile(path, transport=transport)
        except Exception as exc:
            logger.warning("Failed to load profile %r at %s: %s", name, path, exc)
            continue
        registry.profiles[name] = LoadedProfile(name=name, path=path.resolve(), config=cfg)

    if default_target_resolved is not None:
        for p in registry.profiles.values():
            if p.path == default_target_resolved:
                registry.default_name = p.name
                break
    if registry.default_name is None and default_path.is_file() and "default" in registry.profiles:
        registry.default_name = "default"
    if registry.default_name is None and len(registry.profiles) == 1:
        registry.default_name = next(iter(registry.profiles))

    return registry


def single_profile_registry(
    config: Config,
    path: Path | None = None,
    name: str = "default",
) -> ProfileRegistry:
    """Wrap a single Config as a one-entry registry.

    Used by `rocannon mcp serve --profile <path>` (back-compat) and by callers
    that build a Config directly without discovery.
    """
    resolved_path = path.resolve() if path else Path(f"<inline:{name}>")
    return ProfileRegistry(
        profiles={name: LoadedProfile(name=name, path=resolved_path, config=config)},
        default_name=name,
    )


class RuntimeContext:
    """Server-side mutable state: which profile is currently active.

    Constructed once at startup, mutated by the `rocannon_use_profile` tool.
    Cannon tools read `active_config()` on every call.
    """

    def __init__(self, registry: ProfileRegistry, active_name: str | None = None) -> None:
        if active_name is None:
            active_name = registry.default_name
        if active_name is None:
            raise ValueError(
                "no default profile resolved and no active profile passed; "
                "registry has profiles: " + (", ".join(registry.names()) or "(none)")
            )
        if active_name not in registry.profiles:
            raise ValueError(
                f"active profile {active_name!r} not in registry. "
                f"Available: {', '.join(registry.names()) or '(none)'}"
            )
        self.registry = registry
        self._active_name = active_name
        self._lock = asyncio.Lock()
        # Per-profile FQCN module sets. Populated by `register_ansible_modules`
        # after `expand_modules`, read by each tool call to check whether the
        # active profile declares the module being invoked.
        self.expanded_modules: dict[str, set[str]] = {}
        # Per-profile role-name sets, same idea for role tools.
        self.expanded_roles: dict[str, set[str]] = {}

    @property
    def active_name(self) -> str:
        return self._active_name

    def active(self) -> LoadedProfile:
        return self.registry.profiles[self._active_name]

    def active_config(self) -> Config:
        return self.active().config

    def is_module_active(self, module_name: str) -> bool:
        """True if ``module_name`` is declared by the currently-active profile."""
        return module_name in self.expanded_modules.get(self._active_name, set())

    def is_role_active(self, role_name: str) -> bool:
        """True if ``role_name`` is declared by the currently-active profile."""
        return role_name in self.expanded_roles.get(self._active_name, set())

    async def set_active(self, name: str) -> LoadedProfile:
        async with self._lock:
            if name not in self.registry.profiles:
                raise KeyError(
                    f"unknown profile {name!r}. Available: "
                    f"{', '.join(self.registry.names()) or '(none)'}"
                )
            self._active_name = name
            return self.active()
