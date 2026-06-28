from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator, model_validator


class Config(BaseModel):
    """Rocannon server configuration (Ansible-only)."""

    inventories: list[Path] = []
    modules: list[str] = []
    roles: list[str] = []
    roles_path: Path | None = None
    transport: str = "stdio"
    discovery: str = "static"
    timeouts: dict[str, int] = {}
    ansible_cfg: Path | None = None
    vault_password_file: Path | None = None
    extra_envvars: dict[str, str] = {}

    @field_validator("discovery")
    @classmethod
    def discovery_is_known(cls, v: str) -> str:
        """Pre-register every module as a tool (static) or expose a search/describe/run
        trio over the catalog (progressive). Progressive suits large module sets that
        would otherwise flood a client with hundreds of tools."""
        if v not in ("static", "progressive"):
            raise ValueError(f"discovery must be 'static' or 'progressive', got {v!r}")
        return v

    @field_validator("inventories")
    @classmethod
    def inventories_must_exist(cls, v: list[Path]) -> list[Path]:
        """Resolve and validate that all inventory paths exist."""
        resolved = []
        for p in v:
            if not p.exists():
                raise ValueError(f"Inventory file not found: {p}")
            resolved.append(p.resolve())
        return resolved

    @field_validator("ansible_cfg", "vault_password_file")
    @classmethod
    def optional_file_must_exist(cls, v: Path | None) -> Path | None:
        if v is None:
            return None
        if not v.expanduser().exists():
            raise ValueError(f"file not found: {v}")
        return v.expanduser().resolve()

    @field_validator("roles_path")
    @classmethod
    def roles_path_must_exist(cls, v: Path | None) -> Path | None:
        if v is None:
            return None
        if not v.expanduser().exists():
            raise ValueError(f"roles_path not found: {v}")
        return v.expanduser().resolve()

    @model_validator(mode="after")
    def ansible_fully_configured(self) -> "Config":
        """Require an inventory and at least one of modules or roles."""
        has_inv = bool(self.inventories)
        has_tools = bool(self.modules) or bool(self.roles)
        if not (has_inv and has_tools):
            raise ValueError(
                f"Rocannon needs 'inventories' and at least one of 'modules' or "
                f"'roles' (got inventories={self.inventories!r}, "
                f"modules={self.modules!r}, roles={self.roles!r})."
            )
        return self


def load_profile(path: Path, transport: str = "stdio") -> Config:
    """Load a YAML profile file. Relative paths inside the profile resolve
    against the profile file's directory, not the process CWD."""
    raw = yaml.safe_load(path.read_text())
    raw.setdefault("transport", transport)
    base = path.resolve().parent

    def resolve(p: str) -> str:
        pp = Path(p)
        return str(pp if pp.is_absolute() else (base / pp))

    if "inventories" in raw:
        raw["inventories"] = [resolve(p) for p in raw["inventories"]]
    for key in ("ansible_cfg", "vault_password_file", "roles_path"):
        if raw.get(key):
            raw[key] = resolve(raw[key])

    return Config(**raw)
