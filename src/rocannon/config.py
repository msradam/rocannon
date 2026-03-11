from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator, model_validator


class Config(BaseModel):
    """Rocannon server configuration."""

    inventories: list[Path]
    modules: list[str]
    transport: str = "stdio"

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

    @field_validator("modules")
    @classmethod
    def modules_must_not_be_empty(cls, v: list[str]) -> list[str]:
        """Ensure at least one module spec is provided."""
        if not v:
            raise ValueError("At least one module, collection, or namespace is required")
        return v

    @model_validator(mode="after")
    def validate_inventories_not_empty(self) -> "Config":
        """Ensure at least one inventory is provided."""
        if not self.inventories:
            raise ValueError("At least one inventory file is required")
        return self


def load_profile(path: Path) -> Config:
    """Load a YAML profile file into a Config object."""
    raw = yaml.safe_load(path.read_text())
    return Config(**raw)
