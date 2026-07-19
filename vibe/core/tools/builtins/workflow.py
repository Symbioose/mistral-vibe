from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing, suppress
import json
from pathlib import Path
import tempfile
from typing import TYPE_CHECKING, Any
import uuid

from pydantic import BaseModel, Field

from vibe.core.agents.models import AgentIsolation, AgentType, BuiltinAgentName
from vibe.core.config import SessionLoggingConfig, VibeConfig
from vibe.core.logger import logger
from vibe.core.tools.base import (
    BaseTool,
    BaseToolConfig,
    BaseToolState,
    InvokeContext,
    ToolError,
    ToolPermission,
)
from vibe.core.tools.ui import (
    ToolCallDisplay,
    ToolResultDisplay,
    ToolUIData,
    ToolUIDataAdapter,
)
from vibe.core.types import (
    AssistantEvent,
    Role,
    ToolCallEvent,
    ToolResultEvent,
    ToolStreamEvent,
)
from vibe.core.workflow.errors import WorkflowScriptError
from vibe.core.workflow.events import (
    AgentFinishedEvent,
    AgentStartedEvent,
    PhaseStartedEvent,
    WorkflowEvent,
    WorkflowLogEvent,
)
from vibe.core.workflow.journal import WorkflowJournal
from vibe.core.workflow.models import (
    AgentRunStatus,
    SubagentOutcome,
    SubagentRequest,
    WorkflowStatus,
)
from vibe.core.workflow.runtime import WorkflowRuntime
from vibe.core.workflow.script import ParsedScript, parse_workflow_script

if TYPE_CHECKING:
    from vibe.core.agent_loop import AgentLoop

_MAX_RESULT_CHARS = 40_000
_SUBAGENT_PREAMBLE = (
    "You are a subagent inside a deterministic workflow. Your final message IS "
    "the workflow's data — it is consumed by a script, not read by a human. "
    "Return raw findings/data only: no preamble, no summary of what you did, "
    "no offers to help further.\n"
    "You have file tools (grep, read_file) and run inside the user's repository. "
    "Ground EVERY claim in file content you actually read DURING THIS TASK — "
    "never answer from general knowledge or from the brief's description alone. "
    "If the brief names no concrete paths, discover them with grep first, then "
    "read. A claim without a file:line you opened is worthless; drop it.\n\n"
)


class WorkflowArgs(BaseModel):
    script: str | None = Field(
        default=None,
        max_length=10_000,
        description=(
            "Self-contained async Python workflow script starting with a "
            "`meta = {...}` literal. Hard limit 10000 chars / 200 lines: keep it "
            "short mechanical code — every prose agent brief goes in `prompts`, "
            'referenced as prompts["key"]'
        ),
    )
    script_path: str | None = Field(
        default=None,
        description="Path to a workflow script file on disk; takes precedence over `script`",
    )
    args: Any = Field(
        default=None,
        description="Optional JSON value exposed to the script as the global `args`",
    )
    prompts: dict[str, str] | None = Field(
        default=None,
        description='Long agent prompts as a JSON object, referenced in the script as prompts["key"]; ALWAYS put multi-line prose here instead of embedding it in Python strings',
    )
    resume_from_run_id: str | None = Field(
        default=None,
        description="Run ID of a prior workflow invocation to resume from; successful agent() calls with unchanged (prompt, opts) replay instantly",
    )


class WorkflowResult(BaseModel):
    run_id: str
    name: str
    status: WorkflowStatus
    result: Any = None
    error: str | None = None
    agents_spawned: int = 0
    agents_cached: int = 0
    duration_s: float = 0.0


class WorkflowToolConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ASK
    default_agent: str = BuiltinAgentName.EXPLORE
    max_concurrency: int | None = None
    max_agents: int = 1000
    schema_retries: int = 2
    fast_model: str | None = None


class _AgentLoopSpawner:
    def __init__(self, ctx: InvokeContext, config: WorkflowToolConfig) -> None:
        self._ctx = ctx
        self._config = config

    def _build_loop(
        self, agent_name: str, request: SubagentRequest
    ) -> AgentLoop | SubagentOutcome:
        # Deferred: importing AgentLoop at module scope would defeat the TUI's
        # lazy startup (this module is imported by the result-widget registry).
        from vibe.core.agent_loop import AgentLoop
        from vibe.core.config.orchestrator_legacy import LegacyConfigOrchestrator
        from vibe.core.tools.builtins.task import _WRITE_TOOLS

        ctx = self._ctx
        if not ctx.agent_manager:
            return SubagentOutcome(
                success=False, error="workflow requires agent_manager in context"
            )
        try:
            profile = ctx.agent_manager.get_agent(agent_name)
        except ValueError:
            return SubagentOutcome(success=False, error=f"unknown agent: {agent_name}")
        if profile.agent_type != AgentType.SUBAGENT:
            return SubagentOutcome(
                success=False,
                error=f"agent '{agent_name}' is not a subagent; only subagents can run inside workflow",
            )
        enabled_tools = profile.overrides.get("enabled_tools")
        if profile.isolation is not AgentIsolation.NONE or (
            isinstance(enabled_tools, list) and _WRITE_TOOLS & set(enabled_tools)
        ):
            return SubagentOutcome(
                success=False,
                error=(
                    f"agent '{agent_name}' enables write tools or requires worktree "
                    "isolation; workflow runs subagents directly in the checkout "
                    "without isolation — use the task tool for write-capable workers"
                ),
            )

        session_logging = SessionLoggingConfig(
            save_dir=str(ctx.session_dir / "agents") if ctx.session_dir else "",
            session_prefix=f"workflow-{agent_name}",
            enabled=ctx.session_dir is not None,
        )
        load_overrides: dict[str, Any] = {"session_logging": session_logging}
        if request.model:
            load_overrides["active_model"] = request.model
        try:
            base_config = VibeConfig.load(**load_overrides)
            loop = AgentLoop(
                config_orchestrator=LegacyConfigOrchestrator(base_config),
                agent_name=agent_name,
                launch_context=ctx.launch_context,
                is_subagent=True,
                defer_heavy_init=True,
                permission_store=ctx.permission_store,
                hook_config_result=ctx.hook_config_result,
            )
        except Exception as e:
            logger.warning("Workflow subagent '%s' setup failed: %s", agent_name, e)
            return SubagentOutcome(success=False, error=f"subagent setup failed: {e}")
        if ctx.session_id:
            loop.parent_session_id = ctx.session_id
        if ctx.approval_callback:
            loop.set_approval_callback(ctx.approval_callback)
        return loop

    @staticmethod
    def _emit_progress(event: object, on_progress: Callable[[str], None]) -> None:
        if isinstance(event, ToolCallEvent) and event.tool_class:
            adapter = ToolUIDataAdapter(event.tool_class)
            on_progress(f"▸ {adapter.get_call_display(event).summary}")
        elif isinstance(event, ToolResultEvent) and event.result and event.tool_class:
            adapter = ToolUIDataAdapter(event.tool_class)
            display = adapter.get_result_display(event)
            on_progress(f"{event.tool_name}: {display.message}")

    async def run(
        self, request: SubagentRequest, on_progress: Callable[[str], None]
    ) -> SubagentOutcome:
        agent_name = request.agent_name or self._config.default_agent
        # Loop construction does sync config/file IO; off-thread it so N
        # concurrent spawns don't serialize the event loop.
        loop = await asyncio.to_thread(self._build_loop, agent_name, request)
        if isinstance(loop, SubagentOutcome):
            return loop

        prompt = _SUBAGENT_PREAMBLE + request.prompt
        final_message: list[str] = []
        current_message_id: str | None = None
        turns_used = 0
        try:
            async with aclosing(loop.act(prompt)) as events:
                async for event in events:
                    if isinstance(event, AssistantEvent) and event.content:
                        if (
                            event.message_id is not None
                            and event.message_id != current_message_id
                        ):
                            current_message_id = event.message_id
                            final_message.clear()
                        final_message.append(event.content)
                        snippet = event.content.strip().splitlines()
                        if snippet and snippet[0]:
                            on_progress(f"✳ {snippet[0][:90]}")
                    else:
                        self._emit_progress(event, on_progress)
            turns_used = sum(msg.role == Role.assistant for msg in loop.messages)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Workflow subagent '%s' failed: %s", agent_name, e)
            return SubagentOutcome(success=False, error=str(e), turns_used=turns_used)
        finally:
            with suppress(Exception):
                await loop.aclose()

        return SubagentOutcome(
            success=True, text="".join(final_message).strip(), turns_used=turns_used
        )


class Workflow(
    BaseTool[WorkflowArgs, WorkflowResult, WorkflowToolConfig, BaseToolState],
    ToolUIData[WorkflowArgs, WorkflowResult],
):
    @classmethod
    def get_call_display(cls, event: ToolCallEvent) -> ToolCallDisplay:
        args = event.args
        if isinstance(args, WorkflowArgs) and args.script:
            with suppress(WorkflowScriptError):
                meta = parse_workflow_script(args.script).meta
                return ToolCallDisplay(
                    summary=f"Workflow {meta.name}: {meta.description}"
                )
        return ToolCallDisplay(summary="Running workflow")

    @classmethod
    def get_result_display(cls, event: ToolResultEvent) -> ToolResultDisplay:
        result = event.result
        if isinstance(result, WorkflowResult):
            agents = f"{result.agents_spawned} agent{'s' if result.agents_spawned != 1 else ''}"
            cached = f" ({result.agents_cached} cached)" if result.agents_cached else ""
            match result.status:
                case WorkflowStatus.COMPLETED:
                    return ToolResultDisplay(
                        success=True,
                        message=f"Workflow {result.name} completed — {agents}{cached} in {result.duration_s:.0f}s",
                    )
                case WorkflowStatus.CANCELLED:
                    return ToolResultDisplay(
                        success=False,
                        message=f"Workflow {result.name} cancelled after {agents}",
                    )
                case WorkflowStatus.FAILED:
                    return ToolResultDisplay(
                        success=False,
                        message=f"Workflow {result.name} failed: {result.error}",
                    )
        return ToolResultDisplay(success=True, message="Workflow finished")

    @classmethod
    def get_status_text(cls) -> str:
        return "Running workflow"

    async def run(
        self, args: WorkflowArgs, ctx: InvokeContext | None = None
    ) -> AsyncGenerator[ToolStreamEvent | WorkflowResult, None]:
        if not ctx:
            raise ToolError("Workflow tool requires an invocation context")

        source = self._load_source(args)
        prompts = (
            args.prompts if args.prompts is not None else self._sidecar_prompts(args)
        )
        try:
            parsed = parse_workflow_script(source)
        except WorkflowScriptError as e:
            raise ToolError(f"Invalid workflow script: {e}") from e

        run_id = f"wf_{uuid.uuid4().hex[:12]}"
        run_dir = self._runs_dir(ctx) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "script.py").write_text(source, encoding="utf-8")

        journal = self._prepare_journal(ctx, args, run_dir)

        queue: asyncio.Queue[WorkflowEvent | None] = asyncio.Queue()
        runtime = WorkflowRuntime(
            parsed,
            _AgentLoopSpawner(ctx, self.config),
            args=args.args,
            prompts=prompts,
            fast_model=self.config.fast_model,
            on_event=queue.put_nowait,
            journal=journal,
            max_concurrency=self.config.max_concurrency,
            max_agents=self.config.max_agents,
            schema_retries=self.config.schema_retries,
        )
        if (planned := self._phases_planned_event(parsed, ctx)) is not None:
            yield planned

        run_task = asyncio.create_task(runtime.run())
        run_task.add_done_callback(lambda _t: queue.put_nowait(None))

        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                message = _humanize_event(event)
                if message is None:
                    continue
                yield ToolStreamEvent(
                    tool_name=self.get_name(),
                    message=message,
                    tool_call_id=ctx.tool_call_id,
                    data=event.model_dump(mode="json"),
                )
        finally:
            if not run_task.done():
                run_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await run_task

        try:
            outcome = run_task.result()
        except asyncio.CancelledError:
            yield WorkflowResult(
                run_id=run_id, name=parsed.meta.name, status=WorkflowStatus.CANCELLED
            )
            return
        except Exception as e:
            raise ToolError(f"Workflow runtime error: {e}") from e

        result = WorkflowResult(
            run_id=run_id,
            name=parsed.meta.name,
            status=outcome.status,
            result=_bounded_result(outcome.value),
            error=outcome.error,
            agents_spawned=outcome.agents_spawned,
            agents_cached=outcome.agents_cached,
            duration_s=outcome.duration_s,
        )
        with suppress(OSError, TypeError, ValueError):
            (run_dir / "result.json").write_text(
                result.model_dump_json(indent=2), encoding="utf-8"
            )
        yield result

    def get_result_extra(self, result: WorkflowResult) -> str | None:
        if result.status is WorkflowStatus.FAILED and result.error:
            return (
                "The workflow script failed. Fix the script and re-invoke with "
                f'resume_from_run_id="{result.run_id}" — successful agent() calls '
                "will replay from the journal instead of re-running."
            )
        return None

    def _phases_planned_event(
        self, parsed: ParsedScript, ctx: InvokeContext
    ) -> ToolStreamEvent | None:
        if not parsed.meta.phases:
            return None
        return ToolStreamEvent(
            tool_name=self.get_name(),
            message="Plan: " + " → ".join(p.title for p in parsed.meta.phases),
            tool_call_id=ctx.tool_call_id,
            data={
                "kind": "phases_planned",
                "phases": [
                    {"title": p.title, "detail": p.detail} for p in parsed.meta.phases
                ],
            },
        )

    def _prepare_journal(
        self, ctx: InvokeContext, args: WorkflowArgs, run_dir: Path
    ) -> WorkflowJournal:
        resume_journal: Path | None = None
        if args.resume_from_run_id:
            candidate = self._runs_dir(ctx) / args.resume_from_run_id / "journal.jsonl"
            if not candidate.exists():
                raise ToolError(f"No journal found for run '{args.resume_from_run_id}'")
            resume_journal = candidate
        return WorkflowJournal.create(
            run_dir / "journal.jsonl", resume_from=resume_journal
        )

    @staticmethod
    def _sidecar_prompts(args: WorkflowArgs) -> dict[str, str] | None:
        # A script file may ship its briefs alongside: <name>.prompts.json.
        if not args.script_path:
            return None
        sidecar = Path(args.script_path).with_suffix(".prompts.json")
        if not sidecar.is_file():
            return None
        try:
            loaded = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise ToolError(f"Invalid prompts sidecar {sidecar.name}: {e}") from e
        if not isinstance(loaded, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in loaded.items()
        ):
            raise ToolError(
                f"Prompts sidecar {sidecar.name} must be a JSON object of strings"
            )
        return loaded

    @staticmethod
    def _load_source(args: WorkflowArgs) -> str:
        if args.script_path:
            path = Path(args.script_path)
            if not path.is_file():
                raise ToolError(f"Workflow script not found: {args.script_path}")
            return path.read_text(encoding="utf-8")
        if args.script:
            return args.script
        raise ToolError("Provide either `script` or `script_path`")

    @staticmethod
    def _runs_dir(ctx: InvokeContext) -> Path:
        base = ctx.session_dir or ctx.scratchpad_dir
        if base is None:
            base = Path(tempfile.gettempdir()) / "vibe-workflow"
        return Path(base) / "workflow"


def _bounded_result(value: Any) -> Any:
    try:
        serialized = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"truncated": True, "preview": str(value)[:_MAX_RESULT_CHARS]}
    if len(serialized) > _MAX_RESULT_CHARS:
        return {"truncated": True, "preview": serialized[:_MAX_RESULT_CHARS]}
    return value


_STATUS_VERBS = {AgentRunStatus.OK: "done", AgentRunStatus.ERROR: "failed"}


def _humanize_event(event: WorkflowEvent) -> str | None:
    match event:
        case PhaseStartedEvent(title=title):
            message = f"Phase: {title}"
        case AgentStartedEvent(label=label, cached=cached):
            message = (
                f"{label} — replayed from journal" if cached else f"{label} — started"
            )
        case AgentFinishedEvent(label=label, status=status, duration_s=duration) if (
            status in _STATUS_VERBS
        ):
            suffix = f" in {duration:.0f}s" if duration else ""
            message = f"{label} — {_STATUS_VERBS[status]}{suffix}"
        case WorkflowLogEvent(message=message):
            pass
        case _:
            message = None
    return message
