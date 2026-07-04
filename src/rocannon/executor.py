import contextlib
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import ansible_runner
import yaml

from rocannon.redaction import redact, redact_text


def ensure_ansible_on_path() -> None:
    """Put this interpreter's script directory on PATH.

    Rocannon shells out to ansible-doc, ansible-inventory, and ansible-playbook
    (via ansible-runner). When rocannon is launched by absolute path, as MCP
    clients do, with its venv not on PATH, those siblings are not found even
    though they sit next to the running interpreter. Prepend the interpreter's
    bin directory so subprocess lookups resolve them.
    """
    bin_dir = str(Path(sys.executable).parent)
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if bin_dir not in parts:
        os.environ["PATH"] = os.pathsep.join([bin_dir, *parts])


DEFAULT_TIMEOUT = 300
DEFAULT_IDLE_TIMEOUT = 60


def _env_int(name: str, fallback: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return fallback
    try:
        return int(raw)
    except ValueError:
        return fallback


def resolve_timeout() -> int:
    """Resolve the default execution timeout from ``ROCANNON_TIMEOUT`` env var."""
    return _env_int("ROCANNON_TIMEOUT", DEFAULT_TIMEOUT)


def resolve_idle_timeout() -> int:
    """Resolve the default idle timeout from ``ROCANNON_IDLE_TIMEOUT`` env var."""
    return _env_int("ROCANNON_IDLE_TIMEOUT", DEFAULT_IDLE_TIMEOUT)


# Env vars to inherit from the rocannon process into the ansible-runner
# subprocess by default. ZOAU_* covers z/OS Open Automation Utility credentials
# used by `ibm.ibm_zos_core`; users on Z setups rely on these being passed
# through without thinking about it.
_INHERITED_ENV_PREFIXES: tuple[str, ...] = ("ANSIBLE_", "ZOAU_")


def build_envvars(
    extra_envvars: dict[str, str] | None = None,
    ansible_cfg: Path | None = None,
    vault_password_file: Path | None = None,
) -> dict[str, str]:
    """Build the envvars dict passed to ``ansible_runner.run``.

    Precedence (lowest → highest):

    1. Process env vars whose name starts with ``ANSIBLE_`` or ``ZOAU_``.
       Without this, ansible-runner's envvars override drops everything
       inherited from the shell, surprising and breaks ``ANSIBLE_BECOME_PASS``,
       ``ZOAU_HOME``, etc.
    2. Profile fields ``ansible_cfg`` / ``vault_password_file`` mapped to their
       canonical env var names.
    3. Explicit ``extra_envvars`` from the profile, last writer wins.
    """
    env: dict[str, str] = {
        k: v for k, v in os.environ.items() if k.startswith(_INHERITED_ENV_PREFIXES)
    }
    if ansible_cfg is not None:
        env["ANSIBLE_CONFIG"] = str(ansible_cfg)
    if vault_password_file is not None:
        env["ANSIBLE_VAULT_PASSWORD_FILE"] = str(vault_password_file)
    if extra_envvars:
        env.update(extra_envvars)
    return env


def run_module(
    module: str,
    module_args: dict[str, Any],
    inventory: list[str],
    host_pattern: str,
    timeout: int | None = None,
    idle_timeout: int | None = None,
    envvars: dict[str, str] | None = None,
    check: bool = False,
    diff: bool = False,
    execution_environment: str | None = None,
    execution_environment_engine: str = "podman",
    execution_environment_container_options: list[str] | None = None,
) -> dict[str, Any]:
    """Execute an Ansible module via ansible-runner and return structured results.

    ``check`` runs the play in Ansible check mode (dry-run, no changes applied);
    ``diff`` asks modules to report what they would change. Both are play-level
    keywords, gated per module by the caller against ansible-doc support levels.

    ``execution_environment``, when set, dispatches the run into that container
    image instead of the local process, via ansible-runner's own
    ``process_isolation``/``container_image`` support (the same mechanism AWX
    and ``ansible-navigator`` use). ``execution_environment_engine`` selects the
    container engine (``podman`` or ``docker``); ``execution_environment_container_options``
    passes extra args straight to that engine's ``run`` invocation (for example
    ``["--network", "my-compose-net"]`` so the container can reach other
    services by name). Collection Python dependencies then live in the image,
    not in Rocannon's own control-side environment.
    """
    if timeout is None:
        timeout = resolve_timeout()
    if idle_timeout is None:
        idle_timeout = resolve_idle_timeout()
    abs_inventory = []
    for inv_path in inventory:
        p = Path(inv_path)
        if not p.is_absolute():
            p = Path.cwd() / p
        abs_inventory.append(str(p))

    # environment_vars is resolved from inventory by Ansible at runtime
    play: dict[str, Any] = {
        "hosts": host_pattern,
        "gather_facts": False,
        "environment": "{{ environment_vars | default({}) }}",
        "tasks": [
            {
                "name": f"Execute {module}",
                module: module_args or {},
            }
        ],
    }
    if check:
        play["check_mode"] = True
    if diff:
        play["diff"] = True

    private_data_dir: str | None = None
    if execution_environment:
        # ansible-runner bind-mounts private_data_dir into the container and,
        # once containerized, hardcodes the in-container inventory path to
        # private_data_dir/inventory regardless of what `inventory=` is passed
        # (RunnerConfig.prepare_inventory). So both the playbook and every
        # inventory source must live inside private_data_dir to be visible to
        # the container at all; a bare tempfile elsewhere is invisible to it
        # (and, under a VM-backed engine like Colima, may not even be on a
        # host path the VM shares).
        private_data_dir = tempfile.mkdtemp(prefix="rocannon_ee_")
        project_dir = os.path.join(private_data_dir, "project")
        os.makedirs(project_dir, exist_ok=True)
        playbook_path = os.path.join(project_dir, "playbook.yml")
        with open(playbook_path, "w") as f:
            yaml.dump([play], f)

        inventory_dir = os.path.join(private_data_dir, "inventory")
        os.makedirs(inventory_dir, exist_ok=True)
        for src in abs_inventory:
            dest = os.path.join(inventory_dir, os.path.basename(src))
            if os.path.isdir(src):
                shutil.copytree(src, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dest)
    else:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.dump([play], f)
            playbook_path = f.name

    runner_kwargs: dict[str, Any] = {
        "playbook": "playbook.yml" if execution_environment else playbook_path,
        "inventory": abs_inventory,
        "quiet": True,
        "timeout": timeout,
        "settings": {"idle_timeout": idle_timeout},
        "envvars": envvars,
    }
    if execution_environment:
        runner_kwargs.update(
            private_data_dir=private_data_dir,
            process_isolation=True,
            process_isolation_executable=execution_environment_engine,
            container_image=execution_environment,
            container_options=execution_environment_container_options or None,
        )

    try:
        runner = ansible_runner.run(**runner_kwargs)
    except Exception as exc:
        return {
            "status": "error",
            "changed": False,
            "result": {},
            "stdout": "",
            "stderr": redact_text(str(exc)),
        }
    finally:
        with contextlib.suppress(Exception):
            if private_data_dir:
                shutil.rmtree(private_data_dir)
            else:
                Path(playbook_path).unlink()

    result = _parse_runner_result(runner)
    if check:
        result["check_mode"] = True
    return result


def run_role(
    role: str,
    role_args: dict[str, Any],
    inventory: list[str],
    host_pattern: str,
    roles_path: str | None = None,
    timeout: int | None = None,
    idle_timeout: int | None = None,
    envvars: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Execute an Ansible role via ansible-runner's ``run(role=...)``.

    Role arguments are passed as extravars; the role's argument_specs validate
    them at runtime (a missing required arg fails the run). Returns a structured
    result with the per-host stats recap rather than a single module's output.
    """
    if timeout is None:
        timeout = resolve_timeout()
    if idle_timeout is None:
        idle_timeout = resolve_idle_timeout()
    abs_inventory = [
        str(p if (p := Path(inv)).is_absolute() else Path.cwd() / p) for inv in inventory
    ]
    extra: dict[str, Any] = {}
    if roles_path:
        extra["roles_path"] = str(roles_path)
    try:
        runner = ansible_runner.run(
            role=role,
            host_pattern=host_pattern,
            inventory=abs_inventory,
            extravars=role_args or {},
            quiet=True,
            timeout=timeout,
            settings={"idle_timeout": idle_timeout},
            envvars=envvars,
            **extra,
        )
    except Exception as exc:
        return {
            "status": "error",
            "changed": False,
            "result": {},
            "stdout": "",
            "stderr": redact_text(str(exc)),
        }
    return _parse_role_result(runner)


def _parse_role_result(runner: Any) -> dict[str, Any]:
    """Summarize a role run from ansible-runner's stats and failure events."""
    stats = runner.stats or {}
    changed = any((stats.get("changed") or {}).values())
    failed = any((stats.get("failures") or {}).values()) or any((stats.get("dark") or {}).values())
    status = "failed" if (failed or runner.status != "successful") else "successful"
    errors = []
    for event in runner.events:
        if event.get("event") in ("runner_on_failed", "runner_on_unreachable"):
            data = event.get("event_data") or {}
            res = data.get("res") or {}
            errors.append(f"{data.get('host', '?')}: {res.get('msg') or event.get('event')}")
    return {
        "status": status,
        "changed": bool(changed),
        "result": {"stats": stats},
        "stdout": "",
        "stderr": redact_text("\n".join(errors)) if errors else "",
    }


def build_inventory_list(inventory_paths: list[Path]) -> list[str]:
    """Convert inventory Path objects to strings for ansible-runner."""
    return [str(p) for p in inventory_paths]


def _redact_stream(value: Any) -> str:
    """Redact a stdout/stderr field, coercing to text first.

    Some modules (e.g. network modules like arista.eos.eos_command) return
    ``stdout`` as a list of per-command outputs rather than a string.
    """
    if isinstance(value, list):
        value = "\n".join(str(v) for v in value)
    elif not isinstance(value, str):
        value = str(value)
    return redact_text(value)


def _parse_runner_result(runner: Any) -> dict[str, Any]:
    """Extract structured results from ansible-runner events.

    Collects results from all hosts. Returns a single-host format when only
    one host responded, and a per-host format when multiple hosts responded.
    """
    host_results: dict[str, dict[str, Any]] = {}

    for event in runner.events:
        event_data = event.get("event_data", {})
        res = event_data.get("res")
        host = event_data.get("host")
        if res is not None and host is not None:
            host_results[host] = {
                "changed": res.get("changed", False),
                "result": redact(res),
                "stdout": _redact_stream(res.get("stdout", "")),
                "stderr": _redact_stream(res.get("stderr", "")),
            }

    if not host_results:
        # No host produced a result. This happens when the target pattern
        # matched zero hosts ("Could not match supplied host pattern"), the
        # play was skipped, or the runner crashed before any host ran. Report
        # this honestly rather than echoing runner.status="successful", which
        # would mislead callers (LLMs and shell scripts both check status).
        stdout_text = redact_text(runner.stdout.read() if runner.stdout else "")
        stderr_text = redact_text(runner.stderr.read() if runner.stderr else "")
        if runner.status == "successful":
            status = "failed"
            stderr_text = (
                stderr_text
                + "\nrocannon: no host produced a result; the target may not be in the inventory."
            ).strip()
        else:
            status = runner.status
        return {
            "status": status,
            "changed": False,
            "result": {},
            "stdout": stdout_text,
            "stderr": stderr_text,
        }

    status = "failed" if any(_host_failed(h) for h in host_results.values()) else runner.status

    if len(host_results) == 1:
        host_data = next(iter(host_results.values()))
        return {"status": status} | host_data

    return {
        "status": status,
        "changed": any(h["changed"] for h in host_results.values()),
        "hosts": host_results,
    }


def _host_failed(host_entry: dict[str, Any]) -> bool:
    """Return True if this host's Ansible result indicates a failure."""
    res = host_entry.get("result")
    if not isinstance(res, dict):
        return False
    if res.get("failed") is True or res.get("unreachable") is True:
        return True
    rc = res.get("rc")
    return isinstance(rc, int) and rc != 0
