import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, Any

import typer

from rocannon.config import Config, load_profile
from rocannon.correlation import CorrelationFormatter
from rocannon.executor import (
    resolve_idle_timeout,
    resolve_timeout,
    run_module,
)
from rocannon.inventory import load_inventory
from rocannon.playbook import (
    Playbook,
    PlaybookStep,
    load_all_playbooks,
    load_playbook,
)
from rocannon.profiles import (
    ProfileRegistry,
    discover_profiles_dir,
    load_profile_registry,
    single_profile_registry,
)
from rocannon.schema import SchemaFetchError, expand_modules, fetch_module_schema
from rocannon.server import create_server

app = typer.Typer(
    name="rocannon",
    help=(
        "Rocannon: every installed Ansible module as a typed MCP tool and CLI.\n\n"
        "Invoke a module directly with its FQCN:\n"
        "  rocannon ansible.builtin.command --target h1 --cmd 'uptime'\n"
        "  rocannon ansible.builtin.copy --target h1 --src /foo --dest /bar\n"
        "Append --record <file.yml> to write each call into a real Ansible playbook."
    ),
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:
    """Rocannon: every Ansible module as a typed MCP tool and CLI."""


class Transport(StrEnum):
    stdio = "stdio"
    http = "http"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _setup_logging(level: LogLevel) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        CorrelationFormatter("%(name)s %(levelname)s [%(request_id)s] %(message)s")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.value))


def _looks_like_path(value: str) -> bool:
    """Heuristic: treat ``foo/bar``, ``./x.yml``, ``/abs/path.yaml`` as paths.

    A bare token like ``box1`` is treated as a profile name. Anything containing
    a path separator, ending in ``.yml``/``.yaml``, or already existing as a
    file on disk is treated as a path.
    """
    if "/" in value or "\\" in value:
        return True
    if value.endswith((".yml", ".yaml")):
        return True
    return Path(value).is_file()


def _resolve_profile_source(
    inventories: list[Path],
    modules: list[str],
    profile: str | None,
    transport: str,
) -> tuple[ProfileRegistry, str]:
    """Resolve CLI flags into a ``(registry, active_name)`` pair.

    Precedence:
      1. ``--inventory``/``--modules``: build a Config inline; one-entry registry.
      2. ``--profile <path-or-name>``: path → load that file; name → look up in
         the discovered ``.rocannon/profiles/`` registry.
      3. No flags: auto-discover ``.rocannon/profiles/`` and require a default.
    """
    has_flags = bool(inventories or modules)
    if profile and has_flags:
        raise typer.BadParameter("--profile and --inventory/--modules are mutually exclusive.")

    if has_flags:
        cfg = Config(inventories=inventories, modules=modules, transport=transport)
        return single_profile_registry(cfg), "default"

    if profile:
        if _looks_like_path(profile):
            path = Path(profile)
            if not path.is_file():
                raise typer.BadParameter(f"profile file not found: {profile}")
            cfg = load_profile(path, transport=transport)
            name = path.stem
            return single_profile_registry(cfg, path=path, name=name), name
        # Treat as a name: discover and look up.
        profiles_dir = discover_profiles_dir()
        if profiles_dir is None:
            raise typer.BadParameter(
                f"--profile {profile!r}: no .rocannon/profiles/ found in any "
                "parent directory and no ~/.rocannon/profiles/ exists. "
                "Pass a path to a profile YAML, or create the profiles dir."
            )
        registry = load_profile_registry(profiles_dir, transport=transport)
        if profile not in registry.profiles:
            available = ", ".join(registry.names()) or "(none)"
            raise typer.BadParameter(
                f"--profile {profile!r}: not found in {profiles_dir}. Available: {available}"
            )
        return registry, profile

    profiles_dir = discover_profiles_dir()
    if profiles_dir is None:
        raise typer.BadParameter(
            "no --profile, no --inventory/--modules, and no .rocannon/profiles/ "
            "discovered. Provide one of these, or run `rocannon doctor` for help."
        )
    registry = load_profile_registry(profiles_dir, transport=transport)
    if registry.default_name is None:
        available = ", ".join(registry.names()) or "(none)"
        raise typer.BadParameter(
            f"no default profile resolved in {profiles_dir}. Available: {available}. "
            "Symlink or copy one as default.yml, or pass --profile <name>."
        )
    return registry, registry.default_name


def _build_config(
    inventories: list[Path],
    modules: list[str],
    profile: str | Path | None,
    transport: str,
) -> Config:
    """Single-profile shortcut for commands that don't need a registry.

    Used by ``rocannon doctor``, ``ls``, ``playbook run``. Returns the
    resolved ``Config`` for the active profile.
    """
    registry, active = _resolve_profile_source(
        inventories,
        modules,
        str(profile) if profile is not None else None,
        transport,
    )
    return registry.get(active).config


def _parse_arg(raw: str) -> tuple[str, str]:
    """Parse ``key=value`` from the CLI into a (key, value) tuple."""
    if "=" not in raw:
        raise typer.BadParameter(f"Expected key=value, got: {raw!r}")
    key, value = raw.split("=", 1)
    if not key:
        raise typer.BadParameter(f"Empty key in: {raw!r}")
    return key, value


def _pkg_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "not installed"


# ---------------------------------------------------------------------------
# `mcp` subcommands: start the MCP server, doctor the wiring
# ---------------------------------------------------------------------------


def _start_server(
    inventories: list[Path] | None,
    modules: list[str] | None,
    profile: str | None,
    transport: Transport,
    log_level: LogLevel,
) -> None:
    _setup_logging(log_level)
    registry, active = _resolve_profile_source(
        list(inventories or []), list(modules or []), profile, transport.value
    )
    server = create_server(registry, active_name=active)
    server.run(transport=transport.value)


_INV_OPT = typer.Option(
    "--inventory",
    "-i",
    help="Inventory file (repeatable).",
    exists=True,
    dir_okay=False,
    readable=True,
)
_MOD_OPT = typer.Option(
    "--modules",
    "-m",
    help="Module, collection, or namespace (repeatable).",
)
_PROFILE_OPT = typer.Option(
    "--profile",
    "-p",
    help=(
        "Profile to load. Either a path to a YAML file, or a name discovered "
        "in .rocannon/profiles/ (or ~/.rocannon/profiles/). Omit to use the "
        "default profile from the discovered registry."
    ),
)


mcp_app = typer.Typer(
    name="mcp",
    help="MCP server operations: serve, doctor.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(mcp_app)


@mcp_app.command(name="serve")
def mcp_serve(
    inventories: Annotated[list[Path] | None, _INV_OPT] = None,
    modules: Annotated[list[str] | None, _MOD_OPT] = None,
    profile: Annotated[str | None, _PROFILE_OPT] = None,
    transport: Annotated[Transport, typer.Option(help="MCP transport.")] = Transport.stdio,
    log_level: Annotated[
        LogLevel, typer.Option("--log-level", help="Logging level.")
    ] = LogLevel.INFO,
) -> None:
    """Start the Rocannon MCP server."""
    _start_server(inventories, modules, profile, transport, log_level)


@mcp_app.command(name="doctor")
def mcp_doctor(
    inventories: Annotated[list[Path] | None, _INV_OPT] = None,
    modules: Annotated[list[str] | None, _MOD_OPT] = None,
    profile: Annotated[str | None, _PROFILE_OPT] = None,
) -> None:
    """Construct the MCP server in-process and survey its tools, resources, prompts."""
    _setup_logging(LogLevel.WARNING)
    try:
        registry, active = _resolve_profile_source(
            list(inventories or []), list(modules or []), profile, "stdio"
        )
        server = create_server(registry, active_name=active)
    except Exception as exc:
        typer.echo(f"[fail] create_server: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"[ ok ] create_server (active profile: {active}, available: {', '.join(registry.names())})"
    )

    from fastmcp.client import Client

    async def survey() -> tuple[int, int, int, int]:
        async with Client(server) as c:
            tools = await c.list_tools()
            resources = await c.list_resources()
            templates = await c.list_resource_templates()
            prompts = await c.list_prompts()
            return len(tools), len(resources), len(templates), len(prompts)

    n_tools, n_resources, n_templates, n_prompts = asyncio.run(survey())
    typer.echo(f"[ ok ] tools:              {n_tools}")
    typer.echo(f"[ ok ] resources:          {n_resources}")
    typer.echo(f"[ ok ] resource templates: {n_templates}")
    typer.echo(f"[ ok ] prompts:            {n_prompts}")


# ---------------------------------------------------------------------------
# `doctor`, preflight diagnostics
# ---------------------------------------------------------------------------


class _Severity(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


_MARK = {_Severity.OK: "[ ok ]", _Severity.WARN: "[warn]", _Severity.FAIL: "[fail]"}


def _binary_check(name: str) -> tuple[_Severity, str]:
    path = shutil.which(name)
    if not path:
        return _Severity.FAIL, f"{name}: NOT FOUND on PATH"
    try:
        proc = subprocess.run([name, "--version"], capture_output=True, text=True, timeout=10)
        out = proc.stdout or proc.stderr
        first_line = out.splitlines()[0] if out else ""
        return _Severity.OK, f"{name}: {path} ({first_line})"
    except Exception as exc:
        return _Severity.WARN, f"{name}: found at {path} but --version failed: {exc}"


def _inventory_check(inv: Path) -> tuple[_Severity, str]:
    try:
        proc = subprocess.run(
            ["ansible-inventory", "-i", str(inv), "--list"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return _Severity.FAIL, f"{inv}: ansible-inventory not available"
    if proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()[-1] if proc.stderr else "unknown"
        return _Severity.FAIL, f"{inv}: parse failed, {tail}"
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return _Severity.FAIL, f"{inv}: bad JSON from ansible-inventory: {exc}"
    hosts = len(data.get("_meta", {}).get("hostvars", {}))
    if hosts == 0:
        return _Severity.WARN, f"{inv}: parsed OK but 0 hosts"
    return _Severity.OK, f"{inv}: {hosts} host(s)"


def _ping_check(inv: Path, host: str) -> tuple[_Severity, str]:
    try:
        proc = subprocess.run(
            ["ansible", "-i", str(inv), "-m", "ping", host],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return _Severity.WARN, f"{host}: ansible binary unavailable for ping"
    if proc.returncode == 0:
        return _Severity.OK, f"{host}: reachable"
    tail = proc.stderr.strip().splitlines()[-1] if proc.stderr else "unknown"
    return _Severity.FAIL, f"{host}: unreachable, {tail}"


@app.command()
def doctor(
    profile: Annotated[str | None, _PROFILE_OPT] = None,
    inventories: Annotated[list[Path] | None, _INV_OPT] = None,
    ping: Annotated[
        bool,
        typer.Option("--ping/--no-ping", help="Smoke-test SSH connectivity to inventory hosts."),
    ] = False,
) -> None:
    """Preflight checks for Rocannon's dependencies and configuration."""
    rows: list[tuple[_Severity, str, str]] = []

    # Versions
    rows.append(
        (
            _Severity.OK,
            "Versions",
            f"rocannon={_pkg_version('rocannon')} "
            f"fastmcp={_pkg_version('fastmcp')} "
            f"ansible-core={_pkg_version('ansible-core')} "
            f"ansible-runner={_pkg_version('ansible-runner')} "
            f"python={sys.version.split()[0]}",
        )
    )

    # Binaries
    for binary in ("ansible-doc", "ansible-runner", "ansible-inventory", "ansible"):
        sev, msg = _binary_check(binary)
        rows.append((sev, "Binaries", msg))

    # Env knobs
    env_knobs = {k: v for k, v in os.environ.items() if k.startswith(("ROCANNON_", "OTEL_"))}
    env_summary = (
        ", ".join(f"{k}={v}" for k, v in sorted(env_knobs.items())) or "(no ROCANNON_*/OTEL_* set)"
    )
    rows.append((_Severity.OK, "Env", env_summary))
    rows.append(
        (
            _Severity.OK,
            "Timeouts",
            f"timeout={resolve_timeout()}s idle={resolve_idle_timeout()}s",
        )
    )

    # Profile / inventories / Ansible config
    inv_paths: list[Path] = []
    cfg = None
    if profile:
        try:
            cfg = _build_config([], [], profile, "stdio")
            inv_paths = cfg.inventories
            rows.append(
                (
                    _Severity.OK,
                    "Profile",
                    f"{profile}: {len(cfg.modules)} module spec(s), "
                    f"{len(inv_paths)} inventory file(s)",
                )
            )
        except Exception as exc:
            rows.append((_Severity.FAIL, "Profile", f"{profile}: failed to load, {exc}"))
    elif inventories:
        inv_paths = list(inventories)

    # Ansible config: what env will reach the ansible-runner subprocess?
    inherited = sorted(k for k in os.environ if k.startswith(("ANSIBLE_", "ZOAU_")))
    if cfg is not None and cfg.ansible_cfg:
        rows.append((_Severity.OK, "AnsibleCfg", f"ANSIBLE_CONFIG={cfg.ansible_cfg}"))
    else:
        rows.append(
            (
                _Severity.OK,
                "AnsibleCfg",
                "(none in profile; ansible's own discovery applies)",
            )
        )
    if cfg is not None and cfg.vault_password_file:
        rows.append(
            (
                _Severity.OK,
                "Vault",
                f"ANSIBLE_VAULT_PASSWORD_FILE={cfg.vault_password_file}",
            )
        )
    else:
        rows.append((_Severity.OK, "Vault", "(no vault_password_file in profile)"))
    rows.append(
        (
            _Severity.OK,
            "Inherited",
            ", ".join(inherited) if inherited else "(no ANSIBLE_*/ZOAU_* in process env)",
        )
    )
    if cfg is not None and cfg.extra_envvars:
        rows.append(
            (
                _Severity.OK,
                "ExtraEnv",
                ", ".join(f"{k}={v}" for k, v in sorted(cfg.extra_envvars.items())),
            )
        )

    for inv in inv_paths:
        sev, msg = _inventory_check(inv)
        rows.append((sev, "Inventory", msg))

    # Optional connectivity
    if ping and inv_paths:
        for inv in inv_paths:
            try:
                proc = subprocess.run(
                    ["ansible-inventory", "-i", str(inv), "--list"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                hosts = list(json.loads(proc.stdout).get("_meta", {}).get("hostvars", {}).keys())
            except Exception:
                hosts = []
            for host in hosts:
                sev, msg = _ping_check(inv, host)
                rows.append((sev, "Connectivity", msg))

    # Print
    section_width = max(len(s) for _, s, _ in rows)
    for sev, section, msg in rows:
        typer.echo(f"{_MARK[sev]}  {section:<{section_width}}  {msg}")

    if any(sev is _Severity.FAIL for sev, _, _ in rows):
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# `doc`, print parsed schema for a module
# ---------------------------------------------------------------------------


@app.command()
def doc(
    module: Annotated[str, typer.Argument(help="Module FQCN (e.g. ansible.builtin.copy).")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit raw JSON instead of pretty text.")
    ] = False,
) -> None:
    """Print the parsed schema for an Ansible module (the same shape FastMCP sees)."""
    try:
        schema = fetch_module_schema(module)
    except SchemaFetchError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    if as_json:
        typer.echo(json.dumps(schema, indent=2, default=str))
        return

    typer.echo(f"{schema['name']}")
    typer.echo(f"  {schema['description']}")
    typer.echo("")
    if not schema["parameters"]:
        typer.echo("  (no parameters)")
        return
    typer.echo("Parameters:")
    for p in schema["parameters"]:
        flags = []
        if p.get("required"):
            flags.append("required")
        if p.get("type"):
            flags.append(p["type"])
        if "default" in p:
            flags.append(f"default={p['default']!r}")
        if p.get("choices"):
            flags.append(f"choices={p['choices']}")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        typer.echo(f"  - {p['name']}{flag_str}")
        if p.get("description"):
            typer.echo(f"      {p['description']}")


# ---------------------------------------------------------------------------
# `run`, ad-hoc module execution
# ---------------------------------------------------------------------------


@app.command()
def run(
    module: Annotated[str, typer.Argument(help="Module FQCN (e.g. ansible.builtin.ping).")],
    target: Annotated[str, typer.Option("--target", "-t", help="Host or group from inventory.")],
    inventories: Annotated[
        list[Path],
        typer.Option(
            "--inventory",
            "-i",
            help="Inventory file (repeatable, at least one required).",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    args: Annotated[
        list[str] | None,
        typer.Option(
            "--arg",
            "-a",
            help="Module argument as key=value (repeatable). Values are passed as strings.",
        ),
    ] = None,
    args_file: Annotated[
        Path | None,
        typer.Option(
            "--args-file",
            help="JSON file containing a dict of module args (merged with -a).",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    timeout: Annotated[
        int | None, typer.Option(help="Override default execution timeout (seconds).")
    ] = None,
    check: Annotated[
        bool,
        typer.Option("--check", help="Dry-run: report what would change without applying it."),
    ] = False,
    diff: Annotated[
        bool, typer.Option("--diff", help="Return a diff of what this would change.")
    ] = False,
    pretty: Annotated[bool, typer.Option("--pretty", help="Pretty-print JSON output.")] = False,
    log_level: Annotated[
        LogLevel, typer.Option("--log-level", help="Logging level.")
    ] = LogLevel.WARNING,
) -> None:
    """Execute an Ansible module ad-hoc (routes through rocannon's executor)."""
    _setup_logging(log_level)

    if not inventories:
        raise typer.BadParameter("At least one --inventory is required.")

    module_args: dict[str, str | int | float | bool] = {}
    if args_file:
        try:
            module_args.update(json.loads(args_file.read_text()))
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"--args-file is not valid JSON: {exc}") from exc
    for raw in args or []:
        k, v = _parse_arg(raw)
        module_args[k] = v

    result = run_module(
        module=module,
        module_args=module_args,
        inventory=[str(p) for p in inventories],
        host_pattern=target,
        timeout=timeout,
        check=check,
        diff=diff,
    )

    typer.echo(json.dumps(result, indent=2 if pretty else None, default=str))

    status = result.get("status")
    if status == "error":
        raise typer.Exit(code=2)
    if status == "failed":
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# `search`, find modules by name or description
# ---------------------------------------------------------------------------


@app.command()
def search(
    query: Annotated[
        str, typer.Argument(help="Substring to match in module names or descriptions.")
    ],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results.")] = 20,
) -> None:
    """Search Ansible modules by name or description (substring, case-insensitive)."""
    try:
        proc = subprocess.run(
            ["ansible-doc", "--list", "--type", "module", "-j"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError as exc:
        typer.echo("error: ansible-doc not found on PATH", err=True)
        raise typer.Exit(code=2) from exc
    if proc.returncode != 0:
        typer.echo(f"error: ansible-doc failed: {proc.stderr.strip()}", err=True)
        raise typer.Exit(code=2)

    try:
        all_modules: dict[str, str] = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        typer.echo(f"error: bad JSON from ansible-doc: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    pattern = re.compile(re.escape(query), re.IGNORECASE)
    matches = [
        (name, desc or "")
        for name, desc in all_modules.items()
        if pattern.search(name) or pattern.search(desc or "")
    ]
    matches.sort(key=lambda m: m[0])
    for name, desc in matches[:limit]:
        typer.echo(f"{name}")
        if desc:
            typer.echo(f"  {desc}")
    if len(matches) > limit:
        typer.echo(f"\n… {len(matches) - limit} more matches; raise --limit to see all.")


# ---------------------------------------------------------------------------
# `ls`, inspect inventory or modules in this profile
# ---------------------------------------------------------------------------


class LsKind(StrEnum):
    hosts = "hosts"
    groups = "groups"
    modules = "modules"


@app.command(name="ls")
def ls_cmd(
    kind: Annotated[LsKind, typer.Argument(help="What to list.")],
    profile: Annotated[str | None, _PROFILE_OPT] = None,
    inventories: Annotated[list[Path] | None, _INV_OPT] = None,
) -> None:
    """List hosts, groups, or modules from the current profile or inventory files."""
    cfg: Config | None = None
    inv_paths: list[Path] = []
    if profile:
        cfg = _build_config([], [], profile, "stdio")
        inv_paths = cfg.inventories
    elif inventories:
        inv_paths = list(inventories)

    if kind in (LsKind.hosts, LsKind.groups):
        if not inv_paths:
            raise typer.BadParameter("Provide --profile or --inventory to list hosts/groups.")
        inv = load_inventory(inv_paths)
        for item in inv.get(kind.value, []):
            typer.echo(item)
        return

    # modules
    if not cfg:
        raise typer.BadParameter("Provide --profile to list modules.")
    for module_name in expand_modules(cfg.modules):
        typer.echo(module_name)


# ---------------------------------------------------------------------------
# `playbook`, operate on saved .rocannon/playbooks/
# ---------------------------------------------------------------------------


playbook_app = typer.Typer(
    name="playbook",
    help="Manage saved playbooks under .rocannon/playbooks/.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(playbook_app)


@playbook_app.command(name="list")
def playbook_list() -> None:
    """List saved playbooks (from $ROCANNON_DATA_DIR or CWD)."""
    pbs = load_all_playbooks()
    if not pbs:
        typer.echo("(no saved playbooks)")
        return
    for name in sorted(pbs):
        pb = pbs[name]
        step_word = "step" if len(pb.steps) == 1 else "steps"
        typer.echo(f"{name}  ({len(pb.steps)} {step_word})  {pb.description}")


@playbook_app.command(name="show")
def playbook_show(
    name: Annotated[str, typer.Argument(help="Saved playbook name.")],
) -> None:
    """Print the saved playbook YAML (standard Ansible format)."""
    pbs = load_all_playbooks()
    pb = pbs.get(name)
    if pb is None:
        typer.echo(f"error: no saved playbook named {name!r}", err=True)
        raise typer.Exit(code=2)
    typer.echo(pb.to_ansible_yaml())


@playbook_app.command(name="run")
def playbook_run(
    name: Annotated[str, typer.Argument(help="Saved playbook name.")],
    inventories: Annotated[
        list[Path],
        typer.Option(
            "--inventory",
            "-i",
            help="Inventory file (repeatable, at least one required).",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    check: Annotated[
        bool,
        typer.Option(
            "--check", help="Dry-run every step: preview the runbook without applying it."
        ),
    ] = False,
    diff: Annotated[
        bool, typer.Option("--diff", help="Return a diff of what each step would change.")
    ] = False,
    pretty: Annotated[bool, typer.Option("--pretty")] = False,
    log_level: Annotated[
        LogLevel, typer.Option("--log-level", help="Logging level.")
    ] = LogLevel.WARNING,
) -> None:
    """Execute a saved playbook step-by-step against the executor.

    Bypasses the LLM and the MCP transport.
    """
    _setup_logging(log_level)
    pbs = load_all_playbooks()
    pb = pbs.get(name)
    if pb is None:
        typer.echo(f"error: no saved playbook named {name!r}", err=True)
        raise typer.Exit(code=2)

    inventory_strs = [str(p) for p in inventories]
    for i, step in enumerate(pb.steps, 1):
        target = step.args.get("target")
        if not target:
            typer.echo(f"error: step {i} missing 'target' in args", err=True)
            raise typer.Exit(code=2)
        module_args = {k: v for k, v in step.args.items() if k != "target"}
        typer.echo(
            f"--- Step {i}/{len(pb.steps)}: {step.tool} on {target} ---",
            err=True,
        )
        result = run_module(
            module=step.tool,
            module_args=module_args,
            inventory=inventory_strs,
            host_pattern=target,
            check=check,
            diff=diff,
        )
        typer.echo(json.dumps(result, indent=2 if pretty else None, default=str))
        status = result.get("status")
        if status != "successful":
            remaining = len(pb.steps) - i
            typer.echo(
                f"\nstep {i} status={status!r}, halting; {remaining} step(s) not run.",
                err=True,
            )
            raise typer.Exit(code=1 if status == "failed" else 2)


# ---------------------------------------------------------------------------
# `repl`, interactive shell
# ---------------------------------------------------------------------------


@app.command()
def repl(
    inventories: Annotated[list[Path] | None, _INV_OPT] = None,
    modules: Annotated[list[str] | None, _MOD_OPT] = None,
    profile: Annotated[str | None, _PROFILE_OPT] = None,
    log_level: Annotated[
        LogLevel, typer.Option("--log-level", help="Logging level.")
    ] = LogLevel.WARNING,
) -> None:
    """Start the interactive Rocannon shell."""
    _setup_logging(log_level)
    config = _build_config(list(inventories or []), list(modules or []), profile, "stdio")
    from rocannon.repl import Repl

    asyncio.run(Repl(config).start())


# ---------------------------------------------------------------------------
# `rocannon <fqcn>`, dispatch a module as a top-level CLI subcommand
# ---------------------------------------------------------------------------


# Option names reserved by the FQCN-dispatch CLI. If a module parameter
# collides with one of these, the parameter is renamed: a module param
# literally called ``target`` becomes ``--module-target``.
_MODULE_CLI_RESERVED: frozenset[str] = frozenset(
    {
        "target",
        "inventory",
        "profile",
        "record",
        "pretty",
        "timeout",
        "log_level",
        "help",
    }
)


def _looks_like_fqcn(value: str) -> bool:
    """A dotted token that isn't a flag. Used to route argv before Typer."""
    return bool(value) and not value.startswith("-") and "." in value


def _safe_record_name(stem: str) -> str:
    """Coerce an arbitrary file stem into a valid ``Playbook.name``."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", stem).lstrip("-_")
    return safe or "session"


def _append_to_record(path: Path, fqcn: str, target: str, module_args: dict[str, Any]) -> None:
    """Append this invocation to ``path`` as a new play in a real Ansible playbook.

    The file is created if it doesn't exist. The on-disk shape is whatever
    ``Playbook.to_ansible_yaml`` produces, so the resulting file can be run
    directly with ``ansible-playbook -i <inv> <file>``.
    """
    step = PlaybookStep(tool=fqcn, args={**module_args, "target": target})

    if path.exists():
        pb = load_playbook(path)
        pb.steps.append(step)
    else:
        pb = Playbook(name=_safe_record_name(path.stem), description="", steps=[step])

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(pb.to_ansible_yaml())


def _argparse_kwargs_for_type(param: dict[str, Any]) -> dict[str, Any]:
    """Map an ansible-doc parameter type onto argparse ``add_argument`` kwargs.

    Covers the cases we hit in practice: scalar types (str, int, float, path,
    raw) pass through as the corresponding Python type; ``bool`` uses
    ``BooleanOptionalAction`` so ``--wait`` / ``--no-wait`` both work; ``list``
    takes one or more values via ``nargs="+"``; ``dict`` accepts a JSON string.
    """
    atype = param.get("type", "str")
    if atype == "bool":
        return {"action": argparse.BooleanOptionalAction}
    if atype == "int":
        return {"type": int}
    if atype == "float":
        return {"type": float}
    if atype == "list":
        elem = param.get("elements", "str")
        elem_type: type = {"int": int, "float": float}.get(elem, str)
        return {"nargs": "+", "type": elem_type}
    if atype == "dict":
        return {"type": json.loads}
    # str / path / raw fall through to argparse's default str handling.
    return {}


def _normalize_description(raw: Any) -> str:
    """ansible-doc returns parameter descriptions as either a string or a list."""
    if isinstance(raw, list):
        return " ".join(str(line) for line in raw)
    return str(raw) if raw else ""


def _add_module_param(
    parser: argparse.ArgumentParser,
    param: dict[str, Any],
    reserved: set[str],
) -> tuple[str, str]:
    """Add one argparse option from one ansible-doc parameter.

    Returns ``(dest, ansible_name)`` so the caller can translate argparse-side
    dests back to module-side names before invoking the executor.
    """
    ansible_name = param["name"]
    dest = ansible_name
    while dest in reserved:
        dest = f"module_{dest}"
    flag = f"--{dest.replace('_', '-')}"

    description = _normalize_description(param.get("description"))
    required = bool(param.get("required", False))
    default = param.get("default")
    choices = param.get("choices")

    help_text = description
    if not required and default is not None:
        suffix = f"default: {default!r}"
        help_text = f"{description} ({suffix})" if description else suffix

    kwargs: dict[str, Any] = {"dest": dest, "help": help_text}
    kwargs.update(_argparse_kwargs_for_type(param))

    # argparse rejects `required=` together with `BooleanOptionalAction`.
    if required and "action" not in kwargs:
        kwargs["required"] = True
    elif not required and default is not None:
        kwargs["default"] = default

    if isinstance(choices, list) and choices and all(isinstance(c, str) for c in choices):
        kwargs["choices"] = choices

    parser.add_argument(flag, **kwargs)
    return dest, ansible_name


def _build_module_parser(
    fqcn: str, schema: dict[str, Any]
) -> tuple[argparse.ArgumentParser, dict[str, str]]:
    """Build the argparse parser for ``rocannon <fqcn> ...``.

    Adds the Rocannon-reserved flags first (``--target``, ``--inventory``,
    etc.) so they shadow any module-side names that collide. The collision
    set is passed to ``_add_module_param`` for safe renaming.

    Returns the parser plus a ``{dest: ansible_name}`` map for translating
    argparse-side dests back to module-side names.
    """
    parser = argparse.ArgumentParser(
        prog=f"rocannon {fqcn}",
        description=schema.get("description", ""),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--target", "-t", required=True, help="Host or group from inventory.")
    parser.add_argument(
        "--inventory",
        "-i",
        action="append",
        default=[],
        metavar="PATH",
        help="Inventory file (repeatable). Overrides profile discovery.",
    )
    parser.add_argument(
        "--profile",
        "-p",
        help="Profile name (from .rocannon/profiles/) or path to a profile YAML.",
    )
    parser.add_argument(
        "--record",
        metavar="FILE",
        help="Append this invocation to FILE as a new play in a real Ansible playbook.",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    parser.add_argument(
        "--timeout",
        type=int,
        metavar="SECONDS",
        help="Override default execution timeout.",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=[lvl.value for lvl in LogLevel],
        help="Logging level (default: WARNING).",
    )

    attributes = schema.get("attributes", {})
    reserved: set[str] = set(_MODULE_CLI_RESERVED)
    if attributes.get("check_mode") in ("full", "partial"):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Dry-run: report what would change without applying it (Ansible check mode).",
        )
        reserved.add("check")
    if attributes.get("diff_mode") in ("full", "partial"):
        parser.add_argument(
            "--diff",
            action="store_true",
            help="Return a diff of what this would change (Ansible diff mode).",
        )
        reserved.add("diff")
    name_map: dict[str, str] = {}
    for param in schema.get("parameters", []):
        dest, ansible_name = _add_module_param(parser, param, reserved)
        reserved.add(dest)
        name_map[dest] = ansible_name
    return parser, name_map


def _resolve_inventory_paths(inventory_flag: list[str], profile_flag: str | None) -> list[str]:
    """Resolve inventory paths from ``--inventory`` / ``--profile`` / discovery.

    Exits with status 2 and an error message on stderr when nothing resolves.
    """
    if inventory_flag:
        return [str(Path(p).resolve()) for p in inventory_flag]
    try:
        cfg = _build_config([], [], profile_flag, "stdio")
    except typer.BadParameter as exc:
        sys.stderr.write(f"error: {exc}\n")
        sys.exit(2)
    inv_paths = [str(p) for p in cfg.inventories]
    if not inv_paths:
        sys.stderr.write(
            "error: no inventory resolved. Pass --inventory <path>, --profile <name>, "
            "or run from a directory with .rocannon/profiles/default.yml.\n"
        )
        sys.exit(2)
    return inv_paths


def _dispatch_module(fqcn: str, argv: list[str]) -> None:
    """Execute an Ansible module as a CLI subcommand.

    Usage: ``rocannon <fqcn> --target HOST [--module-flag value ...] [--record FILE]``.
    Module schema comes from ``ansible-doc -j``; types map to argparse options
    via ``_argparse_kwargs_for_type``.
    """
    try:
        schema = fetch_module_schema(fqcn)
    except SchemaFetchError as exc:
        sys.stderr.write(f"error: {exc}\n")
        sys.exit(2)

    parser, name_map = _build_module_parser(fqcn, schema)
    ns = parser.parse_args(argv)
    _setup_logging(LogLevel(ns.log_level))

    module_args: dict[str, Any] = {}
    for dest, ansible_name in name_map.items():
        value = getattr(ns, dest, None)
        if value is not None:
            module_args[ansible_name] = value

    inv_paths = _resolve_inventory_paths(ns.inventory, ns.profile)

    result = run_module(
        module=fqcn,
        module_args=module_args,
        inventory=inv_paths,
        host_pattern=ns.target,
        timeout=ns.timeout,
        check=getattr(ns, "check", False),
        diff=getattr(ns, "diff", False),
    )

    if ns.record:
        _append_to_record(Path(ns.record), fqcn, ns.target, module_args)

    sys.stdout.write(json.dumps(result, indent=2 if ns.pretty else None, default=str) + "\n")

    status = result.get("status")
    if status == "error":
        sys.exit(2)
    if status == "failed":
        sys.exit(1)


def main() -> None:
    """CLI entrypoint."""
    # `rocannon <fqcn> ...` bypasses Typer and dispatches the module directly.
    # FQCNs always contain a dot and aren't flags, so they're trivially
    # distinguishable from subcommand names (mcp, doctor, repl, run, ...).
    if len(sys.argv) > 1 and _looks_like_fqcn(sys.argv[1]):
        _dispatch_module(sys.argv[1], sys.argv[2:])
        return
    app()


if __name__ == "__main__":
    main()
