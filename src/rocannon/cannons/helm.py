"""Helm cannon.

For each chart in the profile, registers ``helm_install_<slug>(release_name,
namespace, values, wait)`` plus a ``helm_show_values_<slug>()`` companion.
Server-level utility tools: ``helm_list``, ``helm_status``, ``helm_uninstall``.

Values are passed as an opaque dict; per-chart ``values.schema.json`` (when
present) is left to Helm to validate. Execution uses the ``helm`` CLI against
``$KUBECONFIG`` or ``HelmConfig.kubeconfig``.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from . import Cannon, CannonMetrics, CannonServices

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from rocannon.config import HelmChartSpec, HelmConfig

logger = logging.getLogger("rocannon.helm")

# Slug rule for chart-derived tool names: alphanumerics + underscore.
_SLUG_RE = re.compile(r"[^a-zA-Z0-9_]+")


def _chart_slug(name: str) -> str:
    """``bitnami/redis`` → ``bitnami_redis``."""
    return _SLUG_RE.sub("_", name).strip("_")


def _helm_cmd(kubeconfig: Path | None, *args: str) -> list[str]:
    cmd = ["helm"]
    if kubeconfig:
        cmd += ["--kubeconfig", str(kubeconfig)]
    cmd += list(args)
    return cmd


def _show_chart(name: str, version: str | None, kubeconfig: Path | None) -> dict[str, Any]:
    args = ["show", "chart", name]
    if version:
        args += ["--version", version]
    proc = subprocess.run(
        _helm_cmd(kubeconfig, *args),
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"helm show chart failed: {proc.stderr.strip()}")
    parsed = yaml.safe_load(proc.stdout) or {}
    return parsed if isinstance(parsed, dict) else {}


def _install_or_upgrade(
    chart_spec: HelmChartSpec,
    release_name: str,
    namespace: str,
    values: dict[str, Any] | None,
    kubeconfig: Path | None,
    wait: bool = False,
    timeout: str = "5m",
) -> dict[str, Any]:
    """``helm upgrade --install`` for the chart, returning structured status.

    ``wait=False`` (the default) returns as soon as Helm has applied manifests
    to the cluster, pod readiness is a separate concern from "did the chart
    install succeed." Pass ``wait=True`` for blocking installs (CI gates).
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False,
    ) as f:
        yaml.safe_dump(values or {}, f)
        values_path = f.name

    args = [
        "upgrade", "--install", release_name, chart_spec.name,
        "--namespace", namespace, "--create-namespace",
        "--values", values_path, "--output", "json",
    ]
    if wait:
        args += ["--wait", "--timeout", timeout]
    if chart_spec.version:
        args += ["--version", chart_spec.version]

    try:
        proc = subprocess.run(
            _helm_cmd(kubeconfig, *args),
            capture_output=True, text=True, timeout=600,
        )
    finally:
        Path(values_path).unlink(missing_ok=True)

    if proc.returncode != 0:
        return {
            "ok": False,
            "error": proc.stderr.strip() or proc.stdout.strip(),
            "chart": chart_spec.name,
            "release": release_name,
        }
    try:
        info = json.loads(proc.stdout)
    except json.JSONDecodeError:
        info = {"_raw": proc.stdout}
    return {
        "ok": True,
        "chart": chart_spec.name,
        "release": release_name,
        "namespace": namespace,
        "info": info,
    }


def _helm_list(namespace: str | None, kubeconfig: Path | None) -> list[dict[str, Any]]:
    args = ["list", "--output", "json"]
    if namespace:
        args += ["--namespace", namespace]
    else:
        args += ["--all-namespaces"]
    proc = subprocess.run(
        _helm_cmd(kubeconfig, *args),
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        return []
    try:
        parsed = json.loads(proc.stdout)
        return list(parsed) if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def _helm_status(
    release_name: str, namespace: str, kubeconfig: Path | None
) -> dict[str, Any]:
    proc = subprocess.run(
        _helm_cmd(kubeconfig, "status", release_name,
                  "--namespace", namespace, "--output", "json"),
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()}
    try:
        return dict(json.loads(proc.stdout))
    except json.JSONDecodeError:
        return {"ok": False, "_raw": proc.stdout}


def _helm_uninstall(
    release_name: str, namespace: str, kubeconfig: Path | None
) -> dict[str, Any]:
    proc = subprocess.run(
        _helm_cmd(kubeconfig, "uninstall", release_name, "--namespace", namespace),
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip()}
    return {"ok": True, "release": release_name, "namespace": namespace}


class HelmCannon(Cannon):
    name = "helm"

    def __init__(self, config: HelmConfig) -> None:
        self.config = config

    def register(self, mcp: FastMCP, services: CannonServices) -> CannonMetrics:
        import shutil
        if not shutil.which("helm"):
            raise RuntimeError(
                "Helm cannon needs the `helm` binary on PATH. Install with:\n"
                "  macOS:  brew install helm\n"
                "  Linux:  https://helm.sh/docs/intro/install/\n"
                "  RHEL:   dnf install helm  (or from helm.sh)"
            )

        cfg = self.config
        metrics = CannonMetrics(cannon=self.name)

        for chart in cfg.charts:
            try:
                meta = _show_chart(chart.name, chart.version, cfg.kubeconfig)
                slug = chart.tool_name or _chart_slug(chart.name)
                description = (
                    f"Install or upgrade chart '{chart.name}'"
                    + (f" version {chart.version}" if chart.version else " (latest)")
                    + (f", {meta.get('description', '')}" if meta.get('description') else "")
                )
                self._register_install_tool(mcp, chart, cfg, slug, description)
                self._register_show_values_tool(mcp, chart, cfg, slug)
                metrics.tools_registered += 2
                metrics.tool_names.extend(
                    [f"helm_install_{slug}", f"helm_show_values_{slug}"]
                )
            except Exception as exc:
                metrics.tools_failed.append(chart.name)
                logger.warning("Failed to register helm chart %s: %s", chart.name, exc)

        self._register_utility_tools(mcp, cfg, metrics)
        metrics.extra["charts"] = [c.name for c in cfg.charts]
        return metrics

    def _register_install_tool(
        self,
        mcp: FastMCP,
        chart: HelmChartSpec,
        cfg: HelmConfig,
        slug: str,
        description: str,
    ) -> None:
        # Capture cfg/chart in closure, keep tool signatures clean for the LLM.
        @mcp.tool(
            name=f"helm_install_{slug}",
            description=description,
            tags={"helm", chart.name.split("/", 1)[0]},
        )
        def _install(
            release_name: str,
            namespace: str = cfg.default_namespace,
            values: dict[str, Any] | None = None,
            wait: bool = False,
        ) -> dict[str, Any]:
            return _install_or_upgrade(
                chart, release_name, namespace, values, cfg.kubeconfig, wait=wait,
            )

    def _register_show_values_tool(
        self,
        mcp: FastMCP,
        chart: HelmChartSpec,
        cfg: HelmConfig,
        slug: str,
    ) -> None:
        @mcp.tool(
            name=f"helm_show_values_{slug}",
            description=(
                f"Print the default values.yaml for chart '{chart.name}'. "
                "Use this to discover what you can override in the install tool."
            ),
            tags={"helm", "rocannon.meta"},
        )
        def _show_values() -> str:
            args = ["show", "values", chart.name]
            if chart.version:
                args += ["--version", chart.version]
            proc = subprocess.run(
                _helm_cmd(cfg.kubeconfig, *args),
                capture_output=True, text=True, timeout=60,
            )
            return proc.stdout if proc.returncode == 0 else f"error: {proc.stderr.strip()}"

    def _register_utility_tools(
        self, mcp: FastMCP, cfg: HelmConfig, metrics: CannonMetrics
    ) -> None:
        @mcp.tool(
            name="helm_list",
            description="List Helm releases. Pass namespace to scope; omit for all namespaces.",
            tags={"helm", "rocannon.meta"},
        )
        def _list(namespace: str | None = None) -> list[dict[str, Any]]:
            return _helm_list(namespace, cfg.kubeconfig)

        @mcp.tool(
            name="helm_status",
            description="Get detailed status for a named Helm release.",
            tags={"helm", "rocannon.meta"},
        )
        def _status(
            release_name: str, namespace: str = cfg.default_namespace,
        ) -> dict[str, Any]:
            return _helm_status(release_name, namespace, cfg.kubeconfig)

        @mcp.tool(
            name="helm_uninstall",
            description="Remove a Helm release.",
            tags={"helm", "rocannon.meta"},
        )
        def _uninstall(
            release_name: str, namespace: str = cfg.default_namespace,
        ) -> dict[str, Any]:
            return _helm_uninstall(release_name, namespace, cfg.kubeconfig)

        metrics.tools_registered += 3
        metrics.tool_names.extend(["helm_list", "helm_status", "helm_uninstall"])
