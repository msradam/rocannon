from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, field_validator, model_validator


class TerraformModuleSpec(BaseModel):
    """One Terraform Registry (or local) module to expose as a typed tool."""

    source: str                       # e.g. "terraform-aws-modules/vpc/aws"
    version: str | None = None        # pinned version; None = latest
    tool_name: str | None = None      # override; default derived from source


class TerraformConfig(BaseModel):
    """Configuration for the Terraform cannon."""

    workspace: Path
    providers: dict[str, dict[str, str]] = {}
    provider_config: dict[str, dict[str, Any]] = {}
    expose_resources: list[str] | None = None  # None = all from each provider
    modules: list[TerraformModuleSpec] = []

    @field_validator("workspace")
    @classmethod
    def workspace_path_normalized(cls, v: Path) -> Path:
        return v.expanduser().resolve()


class HelmChartSpec(BaseModel):
    """One chart to expose as a typed install tool."""

    name: str                       # e.g. "bitnami/redis"
    version: str | None = None      # pinned chart version; None = latest
    tool_name: str | None = None    # override; default derived from name

    @field_validator("name")
    @classmethod
    def name_has_slash(cls, v: str) -> str:
        if "/" not in v:
            raise ValueError(f"chart name must be 'repo/chart', got {v!r}")
        return v


class HelmConfig(BaseModel):
    """Configuration for the Helm cannon."""

    charts: list[HelmChartSpec]
    kubeconfig: Path | None = None  # None = default ($KUBECONFIG / ~/.kube/config)
    default_namespace: str = "default"

    @field_validator("kubeconfig")
    @classmethod
    def kubeconfig_exists(cls, v: Path | None) -> Path | None:
        if v is None:
            return None
        v = v.expanduser()
        if not v.exists():
            raise ValueError(f"kubeconfig not found: {v}")
        return v.resolve()


class Config(BaseModel):
    """Rocannon server configuration."""

    # Ansible (legacy top-level fields, implicit AnsibleCannon when populated)
    inventories: list[Path] = []
    modules: list[str] = []
    transport: str = "stdio"
    timeouts: dict[str, int] = {}
    ansible_cfg: Path | None = None
    vault_password_file: Path | None = None
    extra_envvars: dict[str, str] = {}

    # Terraform (optional; instantiates TerraformCannon when set)
    terraform: TerraformConfig | None = None

    # Helm (optional; instantiates HelmCannon when set)
    helm: HelmConfig | None = None

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
    def at_least_one_cannon(self) -> "Config":
        """Require at least one cannon to be configured (Ansible or Terraform)."""
        has_inv = bool(self.inventories)
        has_mods = bool(self.modules)
        if has_inv != has_mods:
            raise ValueError(
                "Partial Ansible config: 'inventories' and 'modules' must both "
                "be set, or both omitted."
            )
        ansible_configured = has_inv and has_mods
        terraform_configured = self.terraform is not None
        helm_configured = self.helm is not None
        if not (ansible_configured or terraform_configured or helm_configured):
            raise ValueError(
                "No cannon configured. Provide at least one of: Ansible "
                "(inventories + modules), Terraform (terraform: ...), "
                "Helm (helm: ...)."
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
    tf = raw.get("terraform") or {}
    if tf.get("workspace"):
        tf["workspace"] = resolve(tf["workspace"])
    helm = raw.get("helm") or {}
    if helm.get("kubeconfig"):
        helm["kubeconfig"] = resolve(helm["kubeconfig"])

    return Config(**raw)
