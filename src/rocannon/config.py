from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator, model_validator


class Config(BaseModel):
    """Rocannon server configuration (Ansible-only)."""

    inventories: list[Path] = []
    modules: list[str] = []
    transport: str = "stdio"
    timeouts: dict[str, int] = {}
    ansible_cfg: Path | None = None
    vault_password_file: Path | None = None
    extra_envvars: dict[str, str] = {}

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

    @model_validator(mode="after")
    def ansible_fully_configured(self) -> "Config":
        """Require both inventories and modules to be set."""
        has_inv = bool(self.inventories)
        has_mods = bool(self.modules)
        if not (has_inv and has_mods):
            raise ValueError(
                f"Rocannon needs both 'inventories' and 'modules' to be set "
                f"(got inventories={self.inventories!r}, modules={self.modules!r})."
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
    for key in ("ansible_cfg", "vault_password_file"):
        if raw.get(key):
            raw[key] = resolve(raw[key])

    return Config(**raw)
