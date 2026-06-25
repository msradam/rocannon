"""Interactive REPL.

Runs ``create_server`` in-process and drives it through ``fastmcp.Client``
over the in-memory transport, so the middleware stack (audit, redaction,
history, response limiting) applies to REPL calls the same way it applies
to MCP-client calls. ``.save`` reads the same ``RunHistory`` that
``commit_session`` does.
"""

from __future__ import annotations

import json
import os
import shlex
from typing import Any

from fastmcp.client import Client
from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory

from rocannon.config import Config
from rocannon.playbook import PLAYBOOK_DIR_NAME, resolve_data_root
from rocannon.server import create_server

PROMPT = "rocannon> "

# Rendered once with: figlet -f nancyj "rocannon"
SPLASH = r"""88d888b. .d8888b. .d8888b. .d8888b. 88d888b. 88d888b. .d8888b. 88d888b.
88'  `88 88'  `88 88'  `"" 88'  `88 88'  `88 88'  `88 88'  `88 88'  `88
88       88.  .88 88.  ... 88.  .88 88    88 88    88 88.  .88 88    88
dP       `88888P' `88888P' `88888P8 dP    dP dP    dP `88888P' dP    dP
"""

DOT_COMMANDS = [
    ".help",
    ".exit",
    ".quit",
    ".target",
    ".inventory",
    ".modules",
    ".doc",
    ".history",
    ".save",
    ".resources",
    ".prompts",
    ".ai",
]

_AI_MAX_STEPS = 8


# Some smaller function-calling models choke on dots in tool names. Mangle the
# dotted FQCNs we register to a double-underscore form for the LLM hop, then
# reverse it before calling the actual tool.
def _mangle(name: str) -> str:
    return name.replace(".", "__")


def _demangle(name: str) -> str:
    return name.replace("__", ".")


_AI_SETUP_HINT = (
    "AI mode requires:\n"
    "  1. pip install 'rocannon[ai]'   (or any environment with litellm installed)\n"
    "  2. ROCANNON_AI_MODEL=<provider>/<model>   "
    "(e.g. watsonx/ibm/granite-3-3-8b-instruct, ollama/granite4.1:3b, openai/gpt-4o)\n"
    "Optional: ROCANNON_AI_BASE_URL, ROCANNON_AI_API_KEY"
)


class _ReplCompleter(Completer):
    """Tab-completion for dot-commands at line start, module names + 'target=' mid-line.

    Kept simple: full per-parameter completion would need the schema for every
    module pre-loaded, which is doable but adds latency. This first pass
    completes what's most useful (module names and the ubiquitous ``target=``).
    """

    def __init__(self, dot_commands: list[str], tool_names: list[str], targets: list[str]) -> None:
        self._dot = sorted(dot_commands)
        self._tools = sorted(tool_names)
        self._targets = sorted(targets)

    def get_completions(self, document: Document, _complete_event: Any) -> Any:
        text_before = document.text_before_cursor
        word = document.get_word_before_cursor(WORD=True)

        if not text_before.strip() or text_before.lstrip() == word:
            # Start of line, complete dot-commands or module names
            for cmd in self._dot:
                if cmd.startswith(word):
                    yield Completion(cmd, start_position=-len(word))
            for tool in self._tools:
                if tool.startswith(word) or word in tool:
                    yield Completion(tool, start_position=-len(word))
            return

        # Mid-line: complete target=<hostname> after `target=`
        if word.startswith("target="):
            prefix = word[len("target=") :]
            for t in self._targets:
                if t.startswith(prefix):
                    yield Completion(f"target={t}", start_position=-len(word))


def _resolve_module(name: str, tool_names: set[str]) -> str:
    """Resolve a short name to a registered FQCN, preferring ``ansible.builtin``."""
    if name in tool_names:
        return name
    suffix_matches = [t for t in tool_names if t.endswith(f".{name}")]
    if len(suffix_matches) == 1:
        return suffix_matches[0]
    builtin = f"ansible.builtin.{name}"
    if builtin in tool_names:
        return builtin
    if suffix_matches:
        raise ValueError(f"ambiguous {name!r}: {suffix_matches[:5]}")
    raise ValueError(f"no module named {name!r} registered")


def _parse_call(line: str) -> tuple[str, dict[str, str]]:
    """Parse ``module key=value key2=value2`` into (module, args_dict).

    Values may be quoted; uses shlex so ``cmd="systemctl status nginx"`` works.
    """
    tokens = shlex.split(line)
    if not tokens:
        raise ValueError("empty input")
    module, *rest = tokens
    args: dict[str, str] = {}
    for raw in rest:
        if "=" not in raw:
            raise ValueError(f"expected key=value, got {raw!r}")
        k, v = raw.split("=", 1)
        if not k:
            raise ValueError(f"empty key in {raw!r}")
        args[k] = v
    return module, args


def _print_ok(text: str) -> None:
    print_formatted_text(FormattedText([("ansigreen", text)]))


def _print_warn(text: str) -> None:
    print_formatted_text(FormattedText([("ansiyellow", text)]))


def _print_err(text: str) -> None:
    print_formatted_text(FormattedText([("ansired", text)]))


def _print_json(payload: Any) -> None:
    print_formatted_text(json.dumps(payload, indent=2, default=str))


class Repl:
    """Interactive shell. One instance per session."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.server = create_server(config)
        self.default_target: str | None = None
        self.tool_names: set[str] = set()
        self.hosts: list[str] = []
        self.groups: list[str] = []

        history_dir = resolve_data_root() / PLAYBOOK_DIR_NAME.split("/")[0]
        history_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = history_dir / "repl_history"

    async def start(self) -> None:
        async with Client(self.server) as client:
            self.client = client
            await self._bootstrap()
            await self._loop()

    async def _bootstrap(self) -> None:
        tools = await self.client.list_tools()
        self.tool_names = {t.name for t in tools}
        inv_res = await self.client.read_resource("rocannon://inventory")
        inv = json.loads(inv_res[0].text)
        self.hosts = list(inv.get("hosts", []))
        self.groups = list(inv.get("groups", []))

        print_formatted_text(FormattedText([("ansicyan", SPLASH)]))
        print_formatted_text("  Every installed Ansible module as a typed MCP tool.")
        print_formatted_text()
        nh, ng = len(self.hosts), len(self.groups)
        _print_ok(
            f"  {len(self.tool_names)} tools, "
            f"{nh} host{'s' if nh != 1 else ''}, "
            f"{ng} group{'s' if ng != 1 else ''} loaded."
        )
        print_formatted_text("  Type .help for commands, .exit to quit.")
        if self.hosts or self.groups:
            sample = ", ".join((self.hosts + self.groups)[:5])
            more = " ..." if nh + ng > 5 else ""
            print_formatted_text(f"  Targets: {sample}{more}")
        print_formatted_text()

    async def _loop(self) -> None:
        completer = _ReplCompleter(DOT_COMMANDS, list(self.tool_names), self.hosts + self.groups)
        session: PromptSession[str] = PromptSession(
            history=FileHistory(str(self.history_path)),
            completer=completer,
        )

        while True:
            try:
                line = await session.prompt_async(PROMPT)
            except (EOFError, KeyboardInterrupt):
                _print_ok("\nbye.")
                return
            line = line.strip()
            if not line:
                continue
            try:
                if line.startswith("."):
                    await self._handle_dot(line)
                else:
                    await self._handle_call(line)
            except Exception as exc:
                _print_err(f"error: {exc}")

    # -- dot commands -------------------------------------------------------

    async def _handle_dot(self, line: str) -> None:
        cmd, _, rest = line.partition(" ")
        rest = rest.strip()

        if cmd in (".exit", ".quit"):
            raise EOFError

        if cmd == ".help":
            self._show_help()
            return

        if cmd == ".target":
            if not rest:
                msg = self.default_target or "(unset)"
                print_formatted_text(f"default target: {msg}")
                return
            if rest not in self.hosts and rest not in self.groups:
                _print_warn(f"warning: {rest!r} is not in the loaded inventory")
            self.default_target = rest
            _print_ok(f"default target → {rest}")
            return

        if cmd == ".inventory":
            hosts_line = ", ".join(self.hosts) or "(none)"
            groups_line = ", ".join(self.groups) or "(none)"
            print_formatted_text(f"Hosts ({len(self.hosts)}): {hosts_line}")
            print_formatted_text(f"Groups ({len(self.groups)}): {groups_line}")
            return

        if cmd == ".modules":
            for t in sorted(self.tool_names):
                print_formatted_text(f"  {t}")
            return

        if cmd == ".doc":
            if not rest:
                raise ValueError("usage: .doc <module>")
            module = _resolve_module(rest, self.tool_names)
            r = await self.client.read_resource(f"rocannon://module/{module}")
            _print_json(json.loads(r[0].text))
            return

        if cmd == ".resources":
            resources = await self.client.list_resources()
            templates = await self.client.list_resource_templates()
            for r in resources:
                print_formatted_text(f"  {r.uri}  ({r.name})")
            for t in templates:
                print_formatted_text(f"  {t.uriTemplate}  ({t.name})")
            return

        if cmd == ".prompts":
            prompts = await self.client.list_prompts()
            if not prompts:
                print_formatted_text("(no prompts registered)")
            for p in prompts:
                print_formatted_text(f"  {p.name} , {p.description or '(no description)'}")
            return

        if cmd == ".history":
            r = await self.client.read_resource("rocannon://runs")
            entries = json.loads(r[0].text)
            if not entries:
                print_formatted_text("(no calls this session)")
                return
            for e in entries[-20:]:
                marker = "ok" if e["status"] == "successful" else e["status"]
                print_formatted_text(
                    f"  {e['request_id']}  {marker:>10}  {e['tool']:<40}  → {e.get('target', '?')}"
                )
            return

        if cmd == ".save":
            tokens = shlex.split(rest)
            if not tokens:
                raise ValueError('usage: .save <name> ["description"]')
            name = tokens[0]
            description = tokens[1] if len(tokens) > 1 else ""
            result = await self.client.call_tool(
                "commit_session",
                {"name": name, "description": description},
            )
            _print_json(json.loads(result.content[0].text))
            return

        if cmd == ".ai":
            if not rest:
                raise ValueError("usage: .ai <prompt>")
            await self._handle_ai(rest)
            return

        raise ValueError(f"unknown command: {cmd}. Type .help.")

    # -- AI passthrough -----------------------------------------------------

    async def _handle_ai(self, prompt: str) -> None:
        """LLM-driven tool-use loop. Tools come from our in-process MCP server."""
        try:
            import litellm  # type: ignore[import-not-found,import-untyped,unused-ignore]
        except ImportError:
            _print_err("litellm not installed")
            for line in _AI_SETUP_HINT.splitlines():
                print_formatted_text(line)
            return

        model = os.environ.get("ROCANNON_AI_MODEL")
        if not model:
            _print_err("ROCANNON_AI_MODEL not set")
            for line in _AI_SETUP_HINT.splitlines():
                print_formatted_text(line)
            return

        api_base = os.environ.get("ROCANNON_AI_BASE_URL")
        api_key = os.environ.get("ROCANNON_AI_API_KEY")

        # Build the OpenAI-shape tool list from our registered MCP tools.
        # FastMCP exposes ``inputSchema`` (JSONSchema) on each Tool, which is
        # exactly what LiteLLM expects under ``parameters``.
        mcp_tools = await self.client.list_tools()
        llm_tools = [
            {
                "type": "function",
                "function": {
                    "name": _mangle(t.name),
                    "description": t.description or "",
                    "parameters": t.inputSchema,
                },
            }
            for t in mcp_tools
        ]

        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

        for _step in range(_AI_MAX_STEPS):
            response = await litellm.acompletion(
                model=model,
                messages=messages,
                tools=llm_tools,
                api_base=api_base,
                api_key=api_key,
            )
            choice = response.choices[0].message
            tool_calls = getattr(choice, "tool_calls", None) or []

            # Record assistant turn (must precede tool results in OpenAI shape).
            messages.append(
                {
                    "role": "assistant",
                    "content": choice.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ]
                    if tool_calls
                    else None,
                }
            )

            if not tool_calls:
                _print_ok("[ai]")
                print_formatted_text(choice.content or "(no content)")
                return

            for tc in tool_calls:
                fn_name = _demangle(tc.function.name)
                raw_args = tc.function.arguments
                try:
                    args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {"_raw": str(raw_args)}
                _print_warn(f"[ai → tool] {fn_name}({args})")
                try:
                    result = await self.client.call_tool(fn_name, args)
                    result_text = result.content[0].text if result.content else "{}"
                except Exception as exc:
                    result_text = json.dumps({"error": str(exc)})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_text,
                    }
                )

        _print_warn(f"[ai] step budget ({_AI_MAX_STEPS}) exhausted")

    # -- module call --------------------------------------------------------

    async def _handle_call(self, line: str) -> None:
        module_name, args = _parse_call(line)
        module = _resolve_module(module_name, self.tool_names)

        if "target" not in args:
            if self.default_target is None:
                raise ValueError(
                    "no `target=` in call and no default set; use .target <name> first"
                )
            args["target"] = self.default_target

        result = await self.client.call_tool(module, args)
        payload = json.loads(result.content[0].text)

        status = payload.get("status", "unknown")
        if status == "successful":
            changed = payload.get("changed", False)
            _print_ok(f"[ok]  changed={changed}")
        elif status == "failed":
            _print_err("[failed]")
        else:
            _print_warn(f"[{status}]")
        _print_json(payload)

    # -- help ---------------------------------------------------------------

    def _show_help(self) -> None:
        print_formatted_text("Module calls:")
        print_formatted_text("  <module> [target=<host>] [key=val ...]")
        print_formatted_text("    e.g.  ping target=webhosts")
        print_formatted_text('          ansible.builtin.command target=h1 cmd="uptime"')
        print_formatted_text("")
        print_formatted_text("Dot commands:")
        print_formatted_text("  .help                       show this")
        print_formatted_text("  .exit / .quit               leave (also ctrl-d, ctrl-c)")
        print_formatted_text("  .target [<name>]            show or set default target")
        print_formatted_text("  .inventory                  list hosts and groups")
        print_formatted_text("  .modules                    list registered module tools")
        print_formatted_text("  .doc <module>               show module schema")
        print_formatted_text("  .resources                  list MCP resources")
        print_formatted_text("  .prompts                    list saved playbook prompts")
        print_formatted_text("  .history                    recent calls this session")
        print_formatted_text('  .save <name> ["desc"]       persist this session as a playbook')
        print_formatted_text("  .ai <prompt>                LLM drives tools (needs rocannon[ai])")
