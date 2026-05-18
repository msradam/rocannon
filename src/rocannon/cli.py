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
from typing import Annotated

import typer
import yaml

from rocannon.config import Config, load_profile
from rocannon.correlation import CorrelationFormatter
from rocannon.executor import (
    resolve_idle_timeout,
    resolve_timeout,
    run_module,
)
from rocannon.inventory import load_inventory
from rocannon.playbook import load_all_playbooks
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
    help="Rocannon, Ansible modules as MCP tools.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:
    """Rocannon, Ansible modules as MCP tools."""


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
    """Single-profile shortcut for commands that don't switch profiles at runtime.

    Used by `rocannon doctor`, `ls`, `playbook run` — commands that just need
    one resolved Config and don't need a multi-profile registry.
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
    """Print the saved playbook YAML."""
    pbs = load_all_playbooks()
    pb = pbs.get(name)
    if pb is None:
        typer.echo(f"error: no saved playbook named {name!r}", err=True)
        raise typer.Exit(code=2)
    typer.echo(yaml.safe_dump(pb.to_dict(), sort_keys=False))


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
    pretty: Annotated[bool, typer.Option("--pretty")] = False,
    log_level: Annotated[
        LogLevel, typer.Option("--log-level", help="Logging level.")
    ] = LogLevel.WARNING,
) -> None:
    """Execute a saved playbook step-by-step (no LLM, no MCP, direct executor calls)."""
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


def main() -> None:
    """CLI entrypoint."""
    app()


if __name__ == "__main__":
    main()
