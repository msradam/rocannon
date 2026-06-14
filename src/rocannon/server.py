import asyncio
import contextlib
import json
import logging
import os
import time
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.context import Context
from fastmcp.server.middleware import Middleware, MiddlewareContext, PingMiddleware
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware, RetryMiddleware
from fastmcp.server.middleware.logging import StructuredLoggingMiddleware
from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware
from mcp.types import PromptListChangedNotification
from starlette.requests import Request
from starlette.responses import JSONResponse

try:
    from opentelemetry import trace as _otel_trace

    _tracer: Any = _otel_trace.get_tracer("rocannon")
except ImportError:
    _tracer = None

from rocannon.config import Config
from rocannon.correlation import (
    init_call_metadata,
    new_request_id,
    reset_request_id,
    set_request_id,
)
from rocannon.history import HistoryEntry, RunHistory
from rocannon.playbook import (
    Playbook,
    PlaybookError,
    PlaybookStep,
    load_all_playbooks,
    save_playbook,
    validate_against_tools,
)
from rocannon.profiles import ProfileRegistry, RuntimeContext, single_profile_registry

logger = logging.getLogger("rocannon")

SERVER_INSTRUCTIONS = """\
Rocannon exposes Ansible modules as MCP tools for remote host automation.

Every tool requires a `target` parameter, a host, group, or pattern from the loaded inventory.
Module parameters are typed tool arguments derived from `ansible-doc`.
Required parameters must be provided; optional parameters have defaults shown in the schema.

Tools execute Ansible modules via SSH. Results include:
- status: "successful" or "failed"
- changed: whether the host state was modified
- result: full Ansible module output
- stdout/stderr: command output when applicable

Use `ansible_builtin_setup` or `ansible_builtin_gather_facts` to discover host details.
Use `ansible_builtin_command` or `ansible_builtin_shell` for ad-hoc commands.
Prefer specific modules (e.g. `ansible_builtin_copy`, `ansible_builtin_file`) over shell commands.\
"""


class _AuditMiddleware(Middleware):
    """Per-call audit log, OTel span, and history record.

    OTel spans are emitted when ``opentelemetry-sdk`` is installed; configure
    the exporter via ``OTEL_EXPORTER_OTLP_ENDPOINT`` or run under
    ``opentelemetry-instrument``. History feeds ``rocannon://runs/{id}`` and
    ``commit_session``.
    """

    def __init__(self, history: RunHistory) -> None:
        self._history = history

    async def on_call_tool(self, context: MiddlewareContext, call_next: Any) -> Any:
        start = time.monotonic()
        tool_name: str = getattr(context.message, "name", "unknown")
        tool_params: dict[str, Any] = dict(getattr(context.message, "arguments", {}) or {})
        target = tool_params.get("target", "unknown")

        request_id = new_request_id()
        token = set_request_id(request_id)
        meta = init_call_metadata()

        span_cm: Any = (
            _tracer.start_as_current_span(f"tools/call {tool_name}")
            if _tracer is not None
            else contextlib.nullcontext()
        )
        try:
            with span_cm as span:
                if span is not None:
                    span.set_attribute("ansible.module", tool_name)
                    span.set_attribute("ansible.target", target)
                    span.set_attribute("rocannon.request_id", request_id)

                result = await call_next(context)

                elapsed_ms = int((time.monotonic() - start) * 1000)
                if span is not None:
                    span.set_attribute("ansible.latency_ms", elapsed_ms)

                # Default to "successful", a tool that returned without raising
                # IS a successful invocation from the MCP-protocol perspective.
                # The Ansible tool fn sets this explicitly via the per-call
                # metadata contextvar to distinguish module-level failures.
                status = str(meta.get("status", "successful"))
                audit_logger.info(
                    json.dumps(
                        {
                            "request_id": request_id,
                            "tool": tool_name,
                            "target": target,
                            "latency_ms": elapsed_ms,
                            "status": status,
                        },
                        default=str,
                    )
                )
                # If tool_fn didn't override args (e.g. for redaction), use the
                # raw arguments from the protocol message.
                recorded_args: dict[str, Any] = meta.get("args") or tool_params
                self._history.record(
                    HistoryEntry(
                        request_id=request_id,
                        tool=tool_name,
                        target=target,
                        status=status,
                        latency_ms=elapsed_ms,
                        args=recorded_args,
                        result=meta.get("result", {}),
                    )
                )
            return result
        finally:
            reset_request_id(token)


class _ConcurrencyMiddleware(Middleware):
    """Two semaphores: global cap and per-target cap.

    Tunables: ``ROCANNON_MAX_CONCURRENT_TOOLS`` (default 10),
    ``ROCANNON_MAX_CONCURRENT_PER_HOST`` (default 3). Per-host is acquired
    before global so a saturated target doesn't hold global slots.
    """

    def __init__(self, max_concurrent: int, max_per_host: int) -> None:
        self._max_concurrent = max_concurrent
        self._max_per_host = max_per_host
        self._global: asyncio.Semaphore | None = None
        self._per_host: dict[str, asyncio.Semaphore] = {}

    def _get_global(self) -> asyncio.Semaphore:
        if self._global is None:
            self._global = asyncio.Semaphore(self._max_concurrent)
        return self._global

    def _get_host(self, target: str) -> asyncio.Semaphore:
        sem = self._per_host.get(target)
        if sem is None:
            sem = asyncio.Semaphore(self._max_per_host)
            self._per_host[target] = sem
        return sem

    async def on_call_tool(self, context: MiddlewareContext, call_next: Any) -> Any:
        args = getattr(context.message, "arguments", {}) or {}
        target = str(args.get("target", "unknown"))
        async with self._get_host(target), self._get_global():
            return await call_next(context)


audit_logger = logging.getLogger("rocannon.audit")


def create_server(
    config_or_registry: Config | ProfileRegistry,
    active_name: str | None = None,
) -> FastMCP:
    """Build a FastMCP server, register every Ansible module, wire profile tools.

    Accepts either a single ``Config`` (back-compat, wraps it as a one-entry
    registry) or a ``ProfileRegistry`` from ``profiles.load_profile_registry``.
    """
    # ``rocannon.ansible`` imports ansible_runner via the executor. Convert
    # an ImportError here into an install-hint message so users running the
    # core package without ``[ansible]`` don't get a cryptic traceback.
    try:
        from rocannon.ansible import register_ansible_modules
    except ImportError as exc:
        raise RuntimeError(
            "Rocannon requires the `ansible` extra: "
            "`pip install 'rocannon[ansible]'` "
            f"(import failed: {exc})"
        ) from exc

    if isinstance(config_or_registry, Config):
        registry = single_profile_registry(config_or_registry)
    else:
        registry = config_or_registry
    runtime = RuntimeContext(registry, active_name=active_name)

    boot_config = runtime.active_config()

    mcp = FastMCP("rocannon", instructions=SERVER_INSTRUCTIONS)
    _add_middlewares(mcp, boot_config)

    history: RunHistory = mcp._rocannon_history  # type: ignore[attr-defined]

    report = register_ansible_modules(mcp, runtime, history)
    logger.info(
        "Registered %d tool(s), %d resource(s); %d failed",
        report.tools_registered,
        report.resources_registered,
        len(report.tools_failed),
    )
    if report.tools_failed:
        preview = ", ".join(report.tools_failed[:10])
        extra_count = len(report.tools_failed) - 10
        suffix = f", ... (+{extra_count} more)" if extra_count > 0 else ""
        logger.warning("Skipped modules: %s%s", preview, suffix)

    all_tool_names: set[str] = set(report.tool_names)
    _add_runs_resources(mcp, history)
    _add_save_tools(mcp, all_tool_names, history)
    _add_profile_tools(mcp, runtime)
    _add_discovery_resources(mcp, runtime)
    all_tool_names.update(
        {
            "save_playbook",
            "commit_session",
            "rocannon_list_profiles",
            "rocannon_current_profile",
            "rocannon_use_profile",
        }
    )
    prompts_registered = _register_playbook_prompts(mcp, all_tool_names)
    if prompts_registered:
        logger.info("Registered %d saved playbook(s) as prompts", prompts_registered)

    health_payload = {
        "status": "ok",
        "tools": report.tools_registered,
        "hosts": report.hosts,
        "groups": report.groups,
        "profiles": registry.names(),
        "active_profile": runtime.active_name,
    }

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({**health_payload, "active_profile": runtime.active_name})

    return mcp


def _add_profile_tools(mcp: FastMCP, runtime: RuntimeContext) -> None:
    """Register the three profile-management tools.

    These let an MCP client (and the LLM driving it) discover available
    profiles, see which is active, and switch between them mid-session
    without restarting the server.
    """

    @mcp.tool(
        name="rocannon_list_profiles",
        description=(
            "List every profile this server knows about. Profiles are loaded "
            "from .rocannon/profiles/*.yml. Returns the names, source paths, "
            "and which one is currently active."
        ),
        tags={"rocannon.meta"},
    )
    def _list_profiles() -> dict[str, Any]:
        return {
            "active": runtime.active_name,
            "default": runtime.registry.default_name,
            "source_dir": str(runtime.registry.source_dir) if runtime.registry.source_dir else None,
            "profiles": [
                {
                    "name": p.name,
                    "path": str(p.path),
                    "inventories": [str(i) for i in p.config.inventories],
                    "modules": list(p.config.modules),
                }
                for p in runtime.registry.profiles.values()
            ],
        }

    @mcp.tool(
        name="rocannon_current_profile",
        description=(
            "Return the active profile's name and resolved configuration "
            "(inventory paths, module list, ansible_cfg, vault settings)."
        ),
        tags={"rocannon.meta"},
    )
    def _current_profile() -> dict[str, Any]:
        active = runtime.active()
        cfg = active.config
        return {
            "name": active.name,
            "path": str(active.path),
            "inventories": [str(i) for i in cfg.inventories],
            "modules": list(cfg.modules),
            "ansible_cfg": str(cfg.ansible_cfg) if cfg.ansible_cfg else None,
            "vault_password_file": (
                str(cfg.vault_password_file) if cfg.vault_password_file else None
            ),
            "extra_envvars": dict(cfg.extra_envvars),
        }

    @mcp.tool(
        name="rocannon_use_profile",
        description=(
            "Switch the active profile. Subsequent Ansible module calls will "
            "use the new profile's inventory, ansible_cfg, vault, and envvars. "
            "The new profile must already be known to this server (see "
            "rocannon_list_profiles); profiles are loaded once at startup."
        ),
        tags={"rocannon.meta"},
    )
    async def _use_profile(name: str) -> dict[str, Any]:
        try:
            active = await runtime.set_active(name)
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "active": active.name,
            "path": str(active.path),
            "inventories": [str(i) for i in active.config.inventories],
            "modules": list(active.config.modules),
        }


def _add_discovery_resources(mcp: FastMCP, runtime: RuntimeContext) -> None:
    """Read-only resources for discovering profiles and saved playbooks."""

    @mcp.resource(
        "rocannon://profiles",
        name="profiles",
        description="Known profiles, their inventories and modules, and which is active.",
        mime_type="application/json",
    )
    def _profiles_resource() -> dict[str, Any]:
        return {
            "active": runtime.active_name,
            "default": runtime.registry.default_name,
            "profiles": [
                {
                    "name": p.name,
                    "path": str(p.path),
                    "inventories": [str(i) for i in p.config.inventories],
                    "modules": list(p.config.modules),
                }
                for p in runtime.registry.profiles.values()
            ],
        }

    @mcp.resource(
        "rocannon://playbooks",
        name="playbooks",
        description="Saved playbooks under .rocannon/playbooks/ (name, description, step count).",
        mime_type="application/json",
    )
    def _playbooks_resource() -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "description": pb.description,
                "steps": len(pb.steps),
                "tools": [s.tool for s in pb.steps],
            }
            for name, pb in sorted(load_all_playbooks().items())
        ]


def _add_middlewares(mcp: FastMCP, config: Config) -> None:
    """Compose the server's middleware stack.

    Order matters, middleware runs in the order added on the way in, reverse on
    the way out. Outer layers see every call; inner layers see only what passed
    the outer checks.
    """
    # 1. ErrorHandling: catches and formats exceptions consistently. Outermost
    #    so it sees errors from every other layer.
    mcp.add_middleware(ErrorHandlingMiddleware(logger=logger, transform_errors=True))

    # 2. Retry: re-runs on transient transport-level exceptions only. Module-level
    #    Ansible failures come back as a dict (not a raise) so they are NOT retried.
    if int(os.environ.get("ROCANNON_RETRY_MAX", "0")):
        mcp.add_middleware(
            RetryMiddleware(
                max_retries=int(os.environ.get("ROCANNON_RETRY_MAX", "0")),
                base_delay=float(os.environ.get("ROCANNON_RETRY_BASE_DELAY", "1.0")),
                retry_exceptions=(ConnectionError, TimeoutError),
                logger=logger,
            )
        )

    # 3. ResponseLimit: cap tool response bytes before they hit the client. Ansible
    #    command/shell stdout can be enormous; an LLM context blown by a single
    #    dump is a real failure mode.
    mcp.add_middleware(
        ResponseLimitingMiddleware(
            max_size=int(os.environ.get("ROCANNON_MAX_RESPONSE_BYTES", "1000000")),
        )
    )

    # 4. Optional structured JSON logging, gated by env so dev defaults stay
    #    human-readable via the CorrelationFormatter wired in cli.py.
    if os.environ.get("ROCANNON_LOG_FORMAT") == "json":
        mcp.add_middleware(StructuredLoggingMiddleware(logger=logger))

    # 5. Ping: only useful for long-lived HTTP sessions. Stdio doesn't need it.
    if config.transport == "http":
        mcp.add_middleware(PingMiddleware())

    # 6. Concurrency cap, innermost layer, closest to the tool. Keeps Ansible
    #    fan-out bounded both globally and per target host.
    max_concurrent = int(os.environ.get("ROCANNON_MAX_CONCURRENT_TOOLS", "10"))
    max_per_host = int(os.environ.get("ROCANNON_MAX_CONCURRENT_PER_HOST", "3"))
    mcp.add_middleware(_ConcurrencyMiddleware(max_concurrent, max_per_host))

    # 7. Audit: emits the structured per-call record with request_id correlation
    #    and feeds the RunHistory used by rocannon://runs/{id} + commit_session.
    #    Innermost so latency reflects only the actual tool work.
    history = getattr(mcp, "_rocannon_history", None) or RunHistory()
    mcp._rocannon_history = history  # type: ignore[attr-defined]
    mcp.add_middleware(_AuditMiddleware(history))


def _playbook_prompt_body(pb: Playbook) -> str:
    """Render a saved playbook as the prompt text shown to the LLM.

    Each step is just ``{tool, args}`` so any registered Ansible module renders
    the same way.
    """
    lines = [
        f"You are about to replay the saved Rocannon playbook '{pb.name}'.",
    ]
    if pb.description:
        lines.append("")
        lines.append(f"Purpose: {pb.description}")
    lines.append("")
    lines.append(
        "Execute these tool calls in order. Do not skip any step. After completing "
        "all steps, summarize what changed for the user."
    )
    lines.append("")
    for i, step in enumerate(pb.steps, 1):
        lines.append(f"Step {i}: call tool `{step.tool}` with arguments:")
        if step.args:
            for k, v in step.args.items():
                lines.append(f"  {k} = {json.dumps(v, default=str)}")
        else:
            lines.append("  (no arguments)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _build_playbook_prompt(name: str, pb: Playbook) -> Any:
    """Build the MCP prompt that replays a saved playbook."""
    from fastmcp.prompts.function_prompt import FunctionPrompt

    body = _playbook_prompt_body(pb)
    prompt_name = f"playbook_{name}"

    def _fn(body: str = body) -> str:
        return body

    _fn.__name__ = prompt_name
    return FunctionPrompt.from_function(
        _fn,
        name=prompt_name,
        description=pb.description or f"Replay saved playbook '{name}'.",
        tags={"rocannon.playbook"},
    )


async def _register_and_notify(mcp: FastMCP, ctx: Context | None, pb: Playbook) -> None:
    """Register a freshly saved playbook as a prompt and tell connected clients.

    This is what lets a recorded session replay immediately, without the
    restart that the on-startup registration in ``_register_playbook_prompts``
    would otherwise require.
    """
    mcp.add_prompt(_build_playbook_prompt(pb.name, pb))
    if ctx is not None:
        await ctx.send_notification(PromptListChangedNotification())


def _register_playbook_prompts(mcp: FastMCP, tool_names: set[str]) -> int:
    """Load .rocannon/playbooks/*.yml and register each as an MCP prompt.

    Validates each playbook against the registered tool names. A playbook
    referencing a tool not in this server's surface is skipped with WARN, never
    registered as a half-broken prompt.
    """
    playbooks = load_all_playbooks()
    if not playbooks:
        return 0

    registered = 0
    for name, pb in playbooks.items():
        problems = validate_against_tools(pb, tool_names)
        if problems:
            logger.warning(
                "Skipping playbook %r, drift from current tools: %s",
                name,
                "; ".join(problems),
            )
            continue
        mcp.add_prompt(_build_playbook_prompt(name, pb))
        registered += 1

    if registered:
        logger.info("Registered %d saved playbook(s) as MCP prompts", registered)
    return registered


def _add_save_tools(
    mcp: FastMCP,
    tool_names: set[str],
    history: RunHistory,
) -> None:
    """Register the playbook recording tools: ``save_playbook`` + ``commit_session``."""

    @mcp.tool(
        name="save_playbook",
        description=(
            "Save a named playbook to .rocannon/playbooks/<name>.yml. "
            "Each step is {tool, args}. The playbook will be available as an "
            "MCP prompt on the next server start. Refuses overwrite unless "
            "overwrite=True."
        ),
        tags={"rocannon.meta"},
    )
    async def _save_playbook_tool(
        name: str,
        description: str,
        steps: list[dict[str, Any]],
        ctx: Context,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        try:
            parsed_steps = [PlaybookStep.from_dict(s) for s in steps]
        except PlaybookError as exc:
            return {"ok": False, "error": str(exc)}
        pb = Playbook(name=name, description=description, steps=parsed_steps)
        problems = validate_against_tools(pb, tool_names)
        if problems:
            return {"ok": False, "error": "tool validation failed", "problems": problems}
        try:
            path = save_playbook(pb, overwrite=overwrite)
        except PlaybookError as exc:
            return {"ok": False, "error": str(exc)}
        await _register_and_notify(mcp, ctx, pb)
        return {
            "ok": True,
            "name": pb.name,
            "path": str(path),
            "steps": len(pb.steps),
            "prompt": f"playbook_{pb.name}",
            "note": f"Available now as the prompt 'playbook_{pb.name}'.",
        }

    @mcp.tool(
        name="commit_session",
        description=(
            "Materialize this session's successful tool calls into a saved "
            "playbook. Pass `since` (a request_id) to skip everything up to "
            "and including that call. Only entries with status='successful' "
            "are included."
        ),
        tags={"rocannon.meta"},
    )
    async def _commit_session_tool(
        name: str,
        ctx: Context,
        description: str = "",
        since: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        entries = history.recent()
        if since:
            try:
                idx = next(i for i, e in enumerate(entries) if e.request_id == since)
                entries = entries[idx + 1 :]
            except StopIteration:
                return {"ok": False, "error": f"no run with request_id={since!r} in history"}
        successful = [e for e in entries if e.status == "successful"]
        if not successful:
            return {"ok": False, "error": "no successful calls to commit"}

        # Don't record the recorders.
        meta_tools = {"save_playbook", "commit_session"}
        successful = [e for e in successful if e.tool not in meta_tools]
        if not successful:
            return {"ok": False, "error": "all candidate calls were meta-tools"}

        steps = [PlaybookStep(tool=e.tool, args=dict(e.args)) for e in successful]
        pb = Playbook(name=name, description=description, steps=steps)
        problems = validate_against_tools(pb, tool_names)
        if problems:
            return {"ok": False, "error": "tool validation failed", "problems": problems}
        try:
            path = save_playbook(pb, overwrite=overwrite)
        except PlaybookError as exc:
            return {"ok": False, "error": str(exc)}
        await _register_and_notify(mcp, ctx, pb)
        return {
            "ok": True,
            "name": pb.name,
            "path": str(path),
            "steps": len(pb.steps),
            "from_request_ids": [e.request_id for e in successful],
            "prompt": f"playbook_{pb.name}",
            "note": f"Available now as the prompt 'playbook_{pb.name}'.",
        }


def _add_runs_resources(mcp: FastMCP, history: RunHistory) -> None:
    """Register the cross-cutting run-history resources.

    The Ansible-specific ``rocannon://inventory`` and ``rocannon://module/<fqcn>``
    resources live in ``rocannon.ansible``.
    """

    @mcp.resource(
        "rocannon://runs",
        name="runs_recent",
        description="Most recent tool call records (request_id, tool, target, status, latency).",
        mime_type="application/json",
    )
    def _runs_recent() -> list[dict[str, Any]]:
        return [e.to_dict() for e in history.recent(limit=50)]

    @mcp.resource(
        "rocannon://runs/{request_id}",
        name="run_detail",
        description="Full record for a single tool call by request_id, including args and result.",
        mime_type="application/json",
    )
    def _run_detail(request_id: str) -> dict[str, Any]:
        entry = history.get(request_id)
        if entry is None:
            return {"error": f"no run with request_id={request_id}"}
        return entry.to_dict()
