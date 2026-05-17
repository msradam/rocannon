import asyncio
import contextlib
import inspect
import json
import keyword
import logging
import os
import re as _re
import time
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.dependencies import CurrentContext
from fastmcp.server.context import Context
from fastmcp.server.middleware import Middleware, MiddlewareContext, PingMiddleware
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware, RetryMiddleware
from fastmcp.server.middleware.logging import StructuredLoggingMiddleware
from fastmcp.server.middleware.response_limiting import ResponseLimitingMiddleware
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

try:
    from opentelemetry import trace as _otel_trace

    _tracer: Any = _otel_trace.get_tracer("rocannon")
except ImportError:
    _tracer = None

from rocannon.config import Config
from rocannon.correlation import (
    get_call_metadata,
    init_call_metadata,
    new_request_id,
    reset_request_id,
    set_request_id,
)
from rocannon.executor import run_module
from rocannon.history import HistoryEntry, RunHistory
from rocannon.playbook import (
    Playbook,
    PlaybookError,
    PlaybookStep,
    load_all_playbooks,
    save_playbook,
    validate_against_tools,
)
from rocannon.redaction import redact
from rocannon.schema import ANSIBLE_TYPE_MAP

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
                # Tool fns that distinguish business success/failure (Ansible,
                # TF) set this explicitly via the per-call metadata contextvar.
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
                # raw arguments from the protocol message, works for any
                # cannon's tool, not just Ansible.
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


def create_server(config: Config) -> FastMCP:
    """Build a FastMCP server, run middleware setup, then invoke each cannon."""
    from rocannon.cannons import Cannon, CannonServices

    # AnsibleCannon imports ansible_runner at module load, fail loudly with a
    # helpful install hint if the user picked a profile that needs it without
    # `pip install rocannon[ansible]`. Terraform/Helm cannons have no Python
    # deps (they shell out), so always importable.
    from rocannon.cannons.helm import HelmCannon
    from rocannon.cannons.terraform import TerraformCannon

    ansible_cls: Any = None
    ansible_import_error: Exception | None = None
    try:
        from rocannon.cannons.ansible import AnsibleCannon
        ansible_cls = AnsibleCannon
    except ImportError as exc:
        ansible_import_error = exc

    mcp = FastMCP(
        "rocannon",
        instructions=SERVER_INSTRUCTIONS,
    )
    _add_middlewares(mcp, config)

    history: RunHistory = mcp._rocannon_history  # type: ignore[attr-defined]
    services = CannonServices(history=history)

    cannons: list[Cannon] = []
    if config.modules:
        if ansible_cls is None:
            raise RuntimeError(
                "Ansible cannon requires the `ansible` extra: "
                "`pip install 'rocannon[ansible]'` "
                f"(import failed: {ansible_import_error})"
            )
        cannons.append(ansible_cls(config))
    if config.terraform is not None:
        cannons.append(TerraformCannon(config.terraform))
    if config.helm is not None:
        cannons.append(HelmCannon(config.helm))

    all_metrics = []
    for cannon in cannons:
        metrics = cannon.register(mcp, services)
        all_metrics.append(metrics)
        logger.info(
            "Cannon '%s' registered: %d tool(s), %d resource(s), %d prompt(s); "
            "%d failed",
            metrics.cannon,
            metrics.tools_registered,
            metrics.resources_registered,
            metrics.prompts_registered,
            len(metrics.tools_failed),
        )
        if metrics.tools_failed:
            preview = ", ".join(metrics.tools_failed[:10])
            extra = len(metrics.tools_failed) - 10
            suffix = f", … (+{extra} more)" if extra > 0 else ""
            logger.warning("Cannon '%s' skipped: %s%s", metrics.cannon, preview, suffix)

    total_tools = sum(m.tools_registered for m in all_metrics)
    if total_tools == 0:
        raise ValueError(
            "No tools registered across all cannons. Check the profile, module "
            "specs, and that the relevant CLI binaries (ansible-doc, tofu, …) "
            "are installed."
        )

    # Union of all tools across all cannons. Used by save/replay validation,
    # commit_session and saved playbook prompts work for any tool name a cannon
    # registered, not just Ansible modules.
    all_tool_names: set[str] = set()
    for m in all_metrics:
        all_tool_names.update(m.tool_names)
    _add_save_tools(mcp, all_tool_names, history)
    # Counting save_playbook + commit_session as additional tools.
    all_tool_names.update({"save_playbook", "commit_session"})
    prompts_registered = _register_playbook_prompts(mcp, all_tool_names)
    if prompts_registered:
        logger.info("Registered %d saved playbook(s) as prompts", prompts_registered)

    # /health: union of all cannons' notable counters.
    health_payload = {
        "status": "ok",
        "tools": total_tools,
        "cannons": {m.cannon: {"tools": m.tools_registered, **m.extra} for m in all_metrics},
    }

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(health_payload)

    return mcp


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

    Generic across cannons, each step is just ``{tool, args}`` so Ansible
    modules, Terraform resources/modules, and Helm charts all render the same.
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


def _register_playbook_prompts(
    mcp: FastMCP, tool_names: set[str]
) -> int:
    """Load .rocannon/playbooks/*.yml and register each as an MCP prompt.

    Validates each playbook against the union of registered tool names across
    all cannons. A playbook referencing a tool not in this server's surface is
    skipped with WARN, never registered as a half-broken prompt.
    """
    from fastmcp.prompts.function_prompt import FunctionPrompt

    playbooks = load_all_playbooks()
    if not playbooks:
        return 0

    registered = 0
    for name, pb in playbooks.items():
        problems = validate_against_tools(pb, tool_names)
        if problems:
            logger.warning(
                "Skipping playbook %r, drift from current tools: %s",
                name, "; ".join(problems),
            )
            continue

        body = _playbook_prompt_body(pb)
        prompt_name = f"playbook_{name}"

        def _fn(body: str = body) -> str:
            return body

        _fn.__name__ = prompt_name
        prompt = FunctionPrompt.from_function(
            _fn,
            name=prompt_name,
            description=pb.description or f"Replay saved playbook '{name}'.",
            tags={"rocannon.playbook"},
        )
        mcp.add_prompt(prompt)
        registered += 1

    if registered:
        logger.info("Registered %d saved playbook(s) as MCP prompts", registered)
    return registered


def _add_save_tools(
    mcp: FastMCP,
    tool_names: set[str],
    history: RunHistory,
) -> None:
    """Register the cross-cannon playbook tools: ``save_playbook`` + ``commit_session``."""

    @mcp.tool(
        name="save_playbook",
        description=(
            "Save a named playbook to .rocannon/playbooks/<name>.yml. "
            "Each step is {tool, args}, works across all cannons (Ansible "
            "modules, Terraform resources/modules, Helm charts). The playbook "
            "will be available as an MCP prompt on the next server start. "
            "Refuses overwrite unless overwrite=True."
        ),
        tags={"rocannon.meta"},
    )
    def _save_playbook_tool(
        name: str,
        description: str,
        steps: list[dict[str, Any]],
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
        return {
            "ok": True,
            "name": pb.name,
            "path": str(path),
            "steps": len(pb.steps),
            "note": "Restart the server to load this playbook as an MCP prompt.",
        }

    @mcp.tool(
        name="commit_session",
        description=(
            "Materialize this session's successful tool calls into a saved "
            "playbook (any cannon, Ansible, Terraform, Helm). Pass `since` "
            "(a request_id) to skip everything up to and including that call. "
            "Only entries with status='successful' are included."
        ),
        tags={"rocannon.meta"},
    )
    def _commit_session_tool(
        name: str,
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

        steps = [
            PlaybookStep(tool=e.tool, args=dict(e.args))
            for e in successful
        ]
        pb = Playbook(name=name, description=description, steps=steps)
        problems = validate_against_tools(pb, tool_names)
        if problems:
            return {"ok": False, "error": "tool validation failed", "problems": problems}
        try:
            path = save_playbook(pb, overwrite=overwrite)
        except PlaybookError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "name": pb.name,
            "path": str(path),
            "steps": len(pb.steps),
            "from_request_ids": [e.request_id for e in successful],
            "note": "Restart the server to load this playbook as an MCP prompt.",
        }


def _add_resources(
    mcp: FastMCP,
    inv: dict[str, list[str]],
    schema_cache: dict[str, dict[str, Any]],
    history: RunHistory,
) -> None:
    """Register read-only MCP resources for inventory, module schemas, and run history."""

    @mcp.resource(
        "rocannon://inventory",
        name="inventory",
        description="Hosts and groups loaded from the configured inventory files.",
        mime_type="application/json",
    )
    def _inventory_resource() -> dict[str, list[str]]:
        return inv

    @mcp.resource(
        "rocannon://module/{fqcn}",
        name="module_schema",
        description="Parsed schema (name, description, parameters) for a registered module.",
        mime_type="application/json",
    )
    def _module_resource(fqcn: str) -> dict[str, Any]:
        schema = schema_cache.get(fqcn)
        if schema is None:
            return {"error": f"module not registered: {fqcn}", "available": sorted(schema_cache)}
        return schema

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


def _collection_tag(module_name: str) -> str:
    """Extract collection name as a tag: 'ansible.builtin.copy' → 'ansible.builtin'."""
    parts = module_name.rsplit(".", 1)
    return parts[0] if len(parts) > 1 else module_name


def _build_target_annotation(inv: dict[str, list[str]]) -> Any:
    """Build a typed annotation for the target parameter.

    Uses Literal for small inventories so the model sees exact valid values.
    Falls back to a described str for larger inventories.
    """
    valid_targets = inv["hosts"] + inv["groups"]
    if len(valid_targets) <= 30:
        return Annotated[
            Literal[tuple(valid_targets)],
            Field(description="Target host or group from inventory"),
        ]
    return Annotated[
        str,
        Field(description=f"Target host or group. Valid: {', '.join(valid_targets)}"),
    ]


def _ansible_type_to_python(param: dict[str, Any]) -> Any:
    """Map an Ansible parameter schema to a Python type for MCP schema generation."""
    atype = param.get("type", "str")
    choices = param.get("choices")

    if choices:
        if isinstance(choices, dict):
            choices = list(choices.keys())
        if isinstance(choices, list) and all(isinstance(c, str) for c in choices):
            return Literal[tuple(choices)]

    base = ANSIBLE_TYPE_MAP.get(atype, str)

    if base is list:
        elem_type = ANSIBLE_TYPE_MAP.get(param.get("elements", "str"), str)
        return list[elem_type]  # type: ignore[valid-type]

    return base


def _register_tool(
    mcp: FastMCP,
    module_name: str,
    schema: dict[str, Any],
    inv: dict[str, list[str]],
    inventory_list: list[str],
    module_timeout: int | None = None,
    envvars: dict[str, str] | None = None,
) -> None:
    """Register a single Ansible module as an MCP tool with typed parameters."""
    fn = _make_tool_fn(module_name, schema, inv, inventory_list, module_timeout, envvars)

    mcp.tool(
        name=module_name,
        description=schema["description"],
        tags={_collection_tag(module_name)},
    )(fn)


def _sanitize_param_name(name: str, reserved: set[str]) -> str:
    """Convert an Ansible parameter name to a valid Python identifier, avoiding collisions."""
    safe = _re.sub(r"[^a-zA-Z0-9_]", "_", name)
    if keyword.iskeyword(safe) or keyword.issoftkeyword(safe):
        safe = f"param_{safe}"
    if safe in reserved:
        safe = f"module_{safe}"
    return safe


def _make_tool_fn(
    module_name: str,
    schema: dict[str, Any],
    inv: dict[str, list[str]],
    inventory_list: list[str],
    module_timeout: int | None = None,
    envvars: dict[str, str] | None = None,
) -> Any:
    """Create an async tool function with a dynamic typed signature matching the Ansible module."""
    target_annotation = _build_target_annotation(inv)
    params = schema["parameters"]

    annotations: dict[str, Any] = {"target": target_annotation}
    sig_params: list[inspect.Parameter] = [
        inspect.Parameter(
            "target",
            inspect.Parameter.KEYWORD_ONLY,
            annotation=target_annotation,
        ),
    ]

    reserved = {"target", "ctx"}
    name_map: dict[str, str] = {}  # python_name → ansible_name
    seen_names: set[str] = set(reserved)

    for p in params:
        ansible_name = p["name"]
        python_name = _sanitize_param_name(ansible_name, reserved)
        while python_name in seen_names:
            python_name = f"{python_name}_"
        seen_names.add(python_name)
        name_map[python_name] = ansible_name

        py_type = _ansible_type_to_python(p)
        is_required = p.get("required", False)
        desc = p.get("description", "")
        default = p.get("default")

        if is_required:
            ann = Annotated[py_type, Field(description=desc)]  # type: ignore[valid-type]
            annotations[python_name] = ann
            sig_params.append(
                inspect.Parameter(
                    python_name,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=ann,
                )
            )
        else:
            optional_type = py_type | None if default is None else py_type
            ann = Annotated[optional_type, Field(description=desc)]  # type: ignore[misc]
            annotations[python_name] = ann
            sig_params.append(
                inspect.Parameter(
                    python_name,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=ann,
                    default=default,
                )
            )

    # Context injection, invisible to the model
    annotations["ctx"] = Context
    sig_params.append(
        inspect.Parameter(
            "ctx",
            inspect.Parameter.KEYWORD_ONLY,
            annotation=Context,
            default=CurrentContext(),
        )
    )

    async def tool_fn(**kwargs: Any) -> str:
        ctx: Context = kwargs.pop("ctx", None)
        target: str = kwargs.pop("target")

        module_args = {name_map.get(k, k): v for k, v in kwargs.items() if v is not None}

        # Hand args back to the audit middleware via the per-call contextvar so
        # the history entry includes them (redacted). The result is already
        # redacted inside the executor.
        meta = get_call_metadata()
        if meta is not None:
            meta["args"] = redact({**module_args, "target": target})

        if ctx and ctx.request_context:
            await ctx.info(f"Executing {module_name} on {target}")
        else:
            logger.info("Executing %s on %s", module_name, target)

        result = await asyncio.to_thread(
            run_module,
            module=module_name,
            module_args=module_args,
            inventory=inventory_list,
            host_pattern=target,
            timeout=module_timeout,
            envvars=envvars,
        )

        if meta is not None:
            meta["result"] = result
            meta["status"] = result.get("status", "ok")

        return json.dumps(result, indent=2, default=str)

    tool_fn.__annotations__ = annotations
    tool_fn.__signature__ = inspect.Signature(sig_params)  # type: ignore[attr-defined]
    tool_fn.__name__ = module_name.replace(".", "_")

    return tool_fn
