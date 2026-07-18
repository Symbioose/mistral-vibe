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

from vibe.core.agents.models import AgentType, BuiltinAgentName
from vibe.core.config import SessionLoggingConfig, VibeConfig
from vibe.core.logger import logger
from vibe.core.mioumioumiou.errors import MiouMiouMiouScriptError
from vibe.core.mioumioumiou.events import (
    AgentFinishedEvent,
    AgentStartedEvent,
    MiouMiouMiouEvent,
    MiouMiouMiouLogEvent,
    PhaseStartedEvent,
)
from vibe.core.mioumioumiou.journal import MiouMiouMiouJournal
from vibe.core.mioumioumiou.models import (
    AgentRunStatus,
    MiouMiouMiouStatus,
    SubagentOutcome,
    SubagentRequest,
)
from vibe.core.mioumioumiou.runtime import MiouMiouMiouRuntime
from vibe.core.mioumioumiou.script import parse_miou_miou_miou_script
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

if TYPE_CHECKING:
    from vibe.core.agent_loop import AgentLoop

_MAX_RESULT_CHARS = 40_000
_SUBAGENT_PREAMBLE = (
    "You are a subagent inside a deterministic miou_miou_miou. Your final message IS "
    "the miou_miou_miou's data — it is consumed by a script, not read by a human. "
    "Return raw findings/data only: no preamble, no summary of what you did, "
    "no offers to help further.\n\n"
)


class MiouMiouMiouArgs(BaseModel):
    script: str | None = Field(
        default=None,
        max_length=10_000,
        description=(
            "Self-contained async Python miou_miou_miou script starting with a "
            "`meta = {...}` literal. Hard limit 10000 chars / 200 lines: keep it "
            "short mechanical code — every prose agent brief goes in `prompts`, "
            'referenced as prompts["key"]'
        ),
    )
    script_path: str | None = Field(
        default=None,
        description="Path to a miou_miou_miou script file on disk; takes precedence over `script`",
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
        description="Run ID of a prior miou_miou_miou invocation to resume from; successful agent() calls with unchanged (prompt, opts) replay instantly",
    )


class MiouMiouMiouResult(BaseModel):
    run_id: str
    name: str
    status: MiouMiouMiouStatus
    result: Any = None
    error: str | None = None
    agents_spawned: int = 0
    agents_cached: int = 0
    duration_s: float = 0.0


class MiouMiouMiouToolConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ASK
    default_agent: str = BuiltinAgentName.EXPLORE
    max_concurrency: int | None = None
    max_agents: int = 1000
    schema_retries: int = 2


class _AgentLoopSpawner:
    def __init__(self, ctx: InvokeContext, config: MiouMiouMiouToolConfig) -> None:
        self._ctx = ctx
        self._config = config

    def _build_loop(
        self, agent_name: str, request: SubagentRequest
    ) -> AgentLoop | SubagentOutcome:
        # Deferred: importing AgentLoop at module scope would defeat the TUI's
        # lazy startup (this module is imported by the result-widget registry).
        from vibe.core.agent_loop import AgentLoop
        from vibe.core.config.orchestrator_legacy import LegacyConfigOrchestrator

        ctx = self._ctx
        if not ctx.agent_manager:
            return SubagentOutcome(
                success=False, error="miou_miou_miou requires agent_manager in context"
            )
        try:
            profile = ctx.agent_manager.get_agent(agent_name)
        except ValueError:
            return SubagentOutcome(success=False, error=f"unknown agent: {agent_name}")
        if profile.agent_type != AgentType.SUBAGENT:
            return SubagentOutcome(
                success=False,
                error=f"agent '{agent_name}' is not a subagent; only subagents can run inside mioumioumiou",
            )

        session_logging = SessionLoggingConfig(
            save_dir=str(ctx.session_dir / "agents") if ctx.session_dir else "",
            session_prefix=f"miou_miou_miou-{agent_name}",
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
            logger.warning("MiouMiouMiou subagent '%s' setup failed: %s", agent_name, e)
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
        loop = self._build_loop(agent_name, request)
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
                    else:
                        self._emit_progress(event, on_progress)
            turns_used = sum(msg.role == Role.assistant for msg in loop.messages)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("MiouMiouMiou subagent '%s' failed: %s", agent_name, e)
            return SubagentOutcome(success=False, error=str(e), turns_used=turns_used)
        finally:
            with suppress(Exception):
                await loop.aclose()

        return SubagentOutcome(
            success=True, text="".join(final_message).strip(), turns_used=turns_used
        )


class MiouMiouMiou(
    BaseTool[
        MiouMiouMiouArgs, MiouMiouMiouResult, MiouMiouMiouToolConfig, BaseToolState
    ],
    ToolUIData[MiouMiouMiouArgs, MiouMiouMiouResult],
):
    @classmethod
    def get_call_display(cls, event: ToolCallEvent) -> ToolCallDisplay:
        args = event.args
        if isinstance(args, MiouMiouMiouArgs) and args.script:
            with suppress(MiouMiouMiouScriptError):
                meta = parse_miou_miou_miou_script(args.script).meta
                return ToolCallDisplay(
                    summary=f"MiouMiouMiou {meta.name}: {meta.description}"
                )
        return ToolCallDisplay(summary="Running miou_miou_miou")

    @classmethod
    def get_result_display(cls, event: ToolResultEvent) -> ToolResultDisplay:
        result = event.result
        if isinstance(result, MiouMiouMiouResult):
            agents = f"{result.agents_spawned} agent{'s' if result.agents_spawned != 1 else ''}"
            cached = f" ({result.agents_cached} cached)" if result.agents_cached else ""
            match result.status:
                case MiouMiouMiouStatus.COMPLETED:
                    return ToolResultDisplay(
                        success=True,
                        message=f"MiouMiouMiou {result.name} completed — {agents}{cached} in {result.duration_s:.0f}s",
                    )
                case MiouMiouMiouStatus.CANCELLED:
                    return ToolResultDisplay(
                        success=False,
                        message=f"MiouMiouMiou {result.name} cancelled after {agents}",
                    )
                case MiouMiouMiouStatus.FAILED:
                    return ToolResultDisplay(
                        success=False,
                        message=f"MiouMiouMiou {result.name} failed: {result.error}",
                    )
        return ToolResultDisplay(success=True, message="MiouMiouMiou finished")

    @classmethod
    def get_status_text(cls) -> str:
        return "Running miou_miou_miou"

    async def run(
        self, args: MiouMiouMiouArgs, ctx: InvokeContext | None = None
    ) -> AsyncGenerator[ToolStreamEvent | MiouMiouMiouResult, None]:
        if not ctx:
            raise ToolError("MiouMiouMiou tool requires an invocation context")

        source = self._load_source(args)
        try:
            parsed = parse_miou_miou_miou_script(source)
        except MiouMiouMiouScriptError as e:
            raise ToolError(f"Invalid miou_miou_miou script: {e}") from e

        run_id = f"miou_{uuid.uuid4().hex[:12]}"
        run_dir = self._runs_dir(ctx) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "script.py").write_text(source, encoding="utf-8")

        resume_journal: Path | None = None
        if args.resume_from_run_id:
            candidate = self._runs_dir(ctx) / args.resume_from_run_id / "journal.jsonl"
            if not candidate.exists():
                raise ToolError(f"No journal found for run '{args.resume_from_run_id}'")
            resume_journal = candidate
        journal = MiouMiouMiouJournal.create(
            run_dir / "journal.jsonl", resume_from=resume_journal
        )

        queue: asyncio.Queue[MiouMiouMiouEvent | None] = asyncio.Queue()
        runtime = MiouMiouMiouRuntime(
            parsed,
            _AgentLoopSpawner(ctx, self.config),
            args=args.args,
            prompts=args.prompts,
            on_event=queue.put_nowait,
            journal=journal,
            max_concurrency=self.config.max_concurrency,
            max_agents=self.config.max_agents,
            schema_retries=self.config.schema_retries,
        )
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
            yield MiouMiouMiouResult(
                run_id=run_id,
                name=parsed.meta.name,
                status=MiouMiouMiouStatus.CANCELLED,
            )
            return
        except Exception as e:
            raise ToolError(f"MiouMiouMiou runtime error: {e}") from e

        result = MiouMiouMiouResult(
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

    def get_result_extra(self, result: MiouMiouMiouResult) -> str | None:
        if result.status is MiouMiouMiouStatus.FAILED and result.error:
            return (
                "The miou_miou_miou script failed. Fix the script and re-invoke with "
                f'resume_from_run_id="{result.run_id}" — successful agent() calls '
                "will replay from the journal instead of re-running."
            )
        return None

    @staticmethod
    def _load_source(args: MiouMiouMiouArgs) -> str:
        if args.script_path:
            path = Path(args.script_path)
            if not path.is_file():
                raise ToolError(f"MiouMiouMiou script not found: {args.script_path}")
            return path.read_text(encoding="utf-8")
        if args.script:
            return args.script
        raise ToolError("Provide either `script` or `script_path`")

    @staticmethod
    def _runs_dir(ctx: InvokeContext) -> Path:
        base = ctx.session_dir or ctx.scratchpad_dir
        if base is None:
            base = Path(tempfile.gettempdir()) / "vibe-mioumioumiou"
        return Path(base) / "mioumioumiou"


def _bounded_result(value: Any) -> Any:
    try:
        serialized = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return {"truncated": True, "preview": str(value)[:_MAX_RESULT_CHARS]}
    if len(serialized) > _MAX_RESULT_CHARS:
        return {"truncated": True, "preview": serialized[:_MAX_RESULT_CHARS]}
    return value


_STATUS_VERBS = {AgentRunStatus.OK: "done", AgentRunStatus.ERROR: "failed"}


def _humanize_event(event: MiouMiouMiouEvent) -> str | None:
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
        case MiouMiouMiouLogEvent(message=message):
            pass
        case _:
            message = None
    return message
