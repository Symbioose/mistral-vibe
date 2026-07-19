from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
import fnmatch
from pathlib import Path
import re
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from vibe.core.agent_loop import AgentLoop
from vibe.core.agents.models import (
    AgentIsolation,
    AgentProfile,
    AgentType,
    BuiltinAgentName,
)
from vibe.core.config import SessionLoggingConfig, VibeConfig
from vibe.core.config.orchestrator_legacy import LegacyConfigOrchestrator
from vibe.core.tools.base import (
    BaseTool,
    BaseToolConfig,
    BaseToolState,
    InvokeContext,
    ToolError,
    ToolPermission,
)
from vibe.core.tools.permissions import PermissionContext
from vibe.core.tools.ui import (
    ToolCallDisplay,
    ToolResultDisplay,
    ToolUIData,
    ToolUIDataAdapter,
)
from vibe.core.types import (
    AgentIdentity,
    AssistantEvent,
    BaseEvent,
    ReasoningEvent,
    Role,
    SubagentFinishedEvent,
    SubagentMergeStatus,
    SubagentStartedEvent,
    ToolCallEvent,
    ToolResultEvent,
    ToolStreamEvent,
)
from vibe.core.worktree import (
    PreparedWorktree,
    WorktreeError,
    commit_worktree,
    merge_branch,
    merge_report,
    prepare_worktree_session,
    remove_worktree,
)

_WRITE_TOOLS = frozenset({"edit", "write_file", "bash", "experimental_bash"})

_FORWARDED_EVENTS = (
    AssistantEvent,
    ReasoningEvent,
    ToolCallEvent,
    ToolResultEvent,
    ToolStreamEvent,
)

_COMMIT_SUBJECT_MAX = 72


class TaskArgs(BaseModel):
    task: str = Field(description="The task for the agent to perform")
    agent: str = Field(
        default="explore",
        description=(
            "The subagent type to use for this task; see Available Subagents "
            "in the system prompt. Use 'worker' for work that changes files, "
            "'explore' for read-only research."
        ),
    )


class TaskResult(BaseModel):
    response: str = Field(description="The accumulated response from the subagent")
    turns_used: int = Field(description="Number of turns the subagent used")
    completed: bool = Field(description="Whether the task completed normally")
    agent_id: str | None = Field(
        default=None, description="Session id of the subagent loop"
    )
    branch: str | None = Field(
        default=None, description="Branch holding the worker's changes"
    )
    worktree_path: str | None = Field(
        default=None, description="Path of the worker's isolated worktree"
    )
    commit: str | None = Field(
        default=None, description="Commit sha of the worker's changes"
    )
    files_changed: list[str] = Field(
        default_factory=list, description="Files changed on the worker's branch"
    )
    merge_status: SubagentMergeStatus = Field(
        default="not_attempted", description="Outcome of the merge-back step"
    )
    conflicting_paths: list[str] = Field(
        default_factory=list,
        description="Paths that would conflict when merging the branch",
    )
    prompt_tokens: int = Field(
        default=0, description="Prompt tokens consumed by the subagent"
    )
    completion_tokens: int = Field(
        default=0, description="Completion tokens consumed by the subagent"
    )


class TaskToolConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ASK
    allowlist: list[str] = Field(
        default=[BuiltinAgentName.EXPLORE, BuiltinAgentName.WORKER]
    )
    max_parallel: int = Field(
        default=4, description="Maximum number of subagents running concurrently."
    )
    merge: Literal["manual", "auto"] = Field(
        default="manual",
        description=(
            "Merge-back policy for worktree-isolated workers: 'auto' merges "
            "conflict-free branches into the invoking checkout, 'manual' leaves "
            "the branch for the caller. Conflicts are always reported, never "
            "auto-resolved."
        ),
    )
    keep_worktrees: Literal["always", "on-failure", "never"] = Field(
        default="on-failure",
        description=(
            "When to keep a worker's worktree on disk after it finishes. The "
            "branch always survives until it is merged."
        ),
    )


class TaskToolState(BaseToolState):
    semaphore: asyncio.Semaphore | None = None


@dataclass
class _WorktreeOutcome:
    branch: str | None = None
    worktree_path: str | None = None
    commit: str | None = None
    files_changed: list[str] = field(default_factory=list)
    conflicting_paths: list[str] = field(default_factory=list)
    merge_status: SubagentMergeStatus = "not_attempted"
    note: str | None = None


class Task(
    BaseTool[TaskArgs, TaskResult, TaskToolConfig, TaskToolState],
    ToolUIData[TaskArgs, TaskResult],
):
    @classmethod
    def get_call_display(cls, event: ToolCallEvent) -> ToolCallDisplay:
        args = event.args
        if isinstance(args, TaskArgs):
            return ToolCallDisplay(summary=f"Running {args.agent} agent: {args.task}")
        return ToolCallDisplay(summary="Running subagent")

    @classmethod
    def get_result_display(cls, event: ToolResultEvent) -> ToolResultDisplay:
        result = event.result
        if isinstance(result, TaskResult):
            turn_word = "turn" if result.turns_used == 1 else "turns"
            suffix = cls._branch_suffix(result)
            if not result.completed:
                return ToolResultDisplay(
                    success=False,
                    message=f"Agent interrupted after {result.turns_used} {turn_word}",
                    suffix=suffix,
                )
            return ToolResultDisplay(
                success=True,
                message=f"Agent completed in {result.turns_used} {turn_word}",
                suffix=suffix,
            )
        return ToolResultDisplay(success=True, message="Agent completed")

    @staticmethod
    def _branch_suffix(result: TaskResult) -> str:
        if not result.branch:
            return ""
        match result.merge_status:
            case "merged":
                return f"[{result.branch}: merged]"
            case "conflicts":
                return f"[{result.branch}: conflicts]"
            case "no_changes":
                return "[no changes]"
            case _:
                return f"[branch {result.branch}]"

    @classmethod
    def get_status_text(cls) -> str:
        return "Running subagent"

    def resolve_permission(self, args: TaskArgs) -> PermissionContext | None:
        agent_name = args.agent

        for pattern in self.config.denylist:
            if fnmatch.fnmatch(agent_name, pattern):
                return PermissionContext(permission=ToolPermission.NEVER)

        for pattern in self.config.allowlist:
            if fnmatch.fnmatch(agent_name, pattern):
                return PermissionContext(permission=ToolPermission.ALWAYS)

        return None

    async def run(
        self, args: TaskArgs, ctx: InvokeContext | None = None
    ) -> AsyncGenerator[BaseEvent | TaskResult, None]:
        if not ctx or not ctx.agent_manager:
            raise ToolError("Task tool requires agent_manager in context")

        agent_profile = self._resolve_profile(args, ctx)

        if self.state.semaphore is None:
            self.state.semaphore = asyncio.Semaphore(self.config.max_parallel)
        async with self.state.semaphore:
            async for item in self._run_subagent(args, ctx, agent_profile):
                yield item

    def _resolve_profile(self, args: TaskArgs, ctx: InvokeContext) -> AgentProfile:
        if ctx.agent_manager is None:
            raise ToolError("Task tool requires agent_manager in context")
        try:
            agent_profile = ctx.agent_manager.get_agent(args.agent)
        except ValueError as e:
            raise ToolError(f"Unknown agent: {args.agent}") from e

        if agent_profile.agent_type != AgentType.SUBAGENT:
            raise ToolError(
                f"Agent '{args.agent}' is a {agent_profile.agent_type.value} agent. "
                f"Only subagents can be used with the task tool. "
                f"This is a security constraint to prevent recursive spawning."
            )

        enabled_tools = agent_profile.overrides.get("enabled_tools")
        if (
            agent_profile.isolation is AgentIsolation.NONE
            and isinstance(enabled_tools, list)
            and _WRITE_TOOLS & set(enabled_tools)
        ):
            raise ToolError(
                f"Agent '{args.agent}' enables write tools but is not isolated. "
                f'Set isolation = "worktree" in its profile so concurrent workers '
                f"cannot corrupt the checkout."
            )

        return agent_profile

    async def _run_subagent(
        self, args: TaskArgs, ctx: InvokeContext, agent_profile: AgentProfile
    ) -> AsyncGenerator[BaseEvent | TaskResult, None]:
        worktree = await self._prepare_isolation(args, ctx, agent_profile)
        subagent_loop = self._build_subagent_loop(args, ctx, worktree)
        identity = AgentIdentity(
            agent_id=str(subagent_loop.session_id),
            parent_id=ctx.session_id,
            name=args.agent,
        )

        yield SubagentStartedEvent(
            tool_call_id=ctx.tool_call_id,
            task=args.task,
            isolation="worktree" if worktree else "none",
            branch=worktree.branch if worktree else None,
            worktree_path=worktree.root if worktree else None,
            agent=identity,
        )

        accumulated_response: list[str] = []
        completed = True
        try:
            async with aclosing(
                subagent_loop.act(self._task_text(args, ctx, worktree))
            ) as events:
                async for event in events:
                    if isinstance(event, AssistantEvent) and event.content:
                        accumulated_response.append(event.content)
                        if event.stopped_by_middleware:
                            completed = False
                    if isinstance(event, ToolResultEvent):
                        if event.skipped:
                            completed = False
                        elif legacy := self._legacy_stream_event(event, ctx):
                            yield legacy
                    if isinstance(event, _FORWARDED_EVENTS):
                        yield event.model_copy(update={"agent": identity})
        except Exception as e:
            completed = False
            accumulated_response.append(f"\n[Subagent error: {e}]")
        finally:
            with suppress(Exception):
                await subagent_loop.aclose()

        turns_used = sum(msg.role == Role.assistant for msg in subagent_loop.messages)
        prompt_tokens = int(subagent_loop.stats.session_prompt_tokens)
        completion_tokens = int(subagent_loop.stats.session_completion_tokens)

        outcome = await self._finalize_worktree(worktree, args, completed=completed)
        if outcome.note:
            accumulated_response.append(f"\n[Worktree: {outcome.note}]")

        yield SubagentFinishedEvent(
            tool_call_id=ctx.tool_call_id,
            status="completed" if completed else "failed",
            turns_used=turns_used,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            branch=outcome.branch,
            merge_status=outcome.merge_status,
            agent=identity,
        )

        yield TaskResult(
            response="".join(accumulated_response),
            turns_used=turns_used,
            completed=completed,
            agent_id=identity.agent_id,
            branch=outcome.branch,
            worktree_path=outcome.worktree_path,
            commit=outcome.commit,
            files_changed=outcome.files_changed,
            merge_status=outcome.merge_status,
            conflicting_paths=outcome.conflicting_paths,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    async def _prepare_isolation(
        self, args: TaskArgs, ctx: InvokeContext, agent_profile: AgentProfile
    ) -> PreparedWorktree | None:
        if agent_profile.isolation is not AgentIsolation.WORKTREE:
            return None

        base = ctx.working_dir or Path.cwd()
        name = f"vibe-{_sanitize(args.agent)}-{_sanitize(ctx.tool_call_id)[:8]}"
        try:
            return await asyncio.to_thread(prepare_worktree_session, name, base)
        except WorktreeError as e:
            raise ToolError(
                f"Agent '{args.agent}' requires worktree isolation: {e}"
            ) from e

    def _build_subagent_loop(
        self, args: TaskArgs, ctx: InvokeContext, worktree: PreparedWorktree | None
    ) -> AgentLoop:
        session_logging = SessionLoggingConfig(
            save_dir=str(ctx.session_dir / "agents") if ctx.session_dir else "",
            session_prefix=args.agent,
            enabled=ctx.session_dir is not None,
        )
        base_config = VibeConfig.load(session_logging=session_logging)
        if worktree is not None:
            _apply_sandbox_overrides(base_config, worktree)

        subagent_loop = AgentLoop(
            config_orchestrator=LegacyConfigOrchestrator(base_config),
            agent_name=args.agent,
            launch_context=ctx.launch_context,
            is_subagent=True,
            defer_heavy_init=True,
            permission_store=ctx.permission_store,
            hook_config_result=ctx.hook_config_result,
            working_dir=worktree.path if worktree else None,
            force_bypass_tool_permissions=ctx.bypass_tool_permissions,
        )
        if ctx.session_id:
            subagent_loop.parent_session_id = ctx.session_id
        if ctx.approval_callback:
            subagent_loop.set_approval_callback(ctx.approval_callback)
        return subagent_loop

    def _task_text(
        self, args: TaskArgs, ctx: InvokeContext, worktree: PreparedWorktree | None
    ) -> str:
        parts: list[str] = []
        if worktree is not None:
            base = ctx.working_dir or Path.cwd()
            parts.append(
                f"Working directory: {worktree.path}\n"
                f"You are in an isolated git worktree on branch "
                f"'{worktree.branch}'. Always use relative paths or paths under "
                f"your working directory — writes outside it are denied. If the "
                f"task mentions absolute paths under {base}, operate on the same "
                f"relative paths inside your working directory instead. Do not "
                f"run git add/commit/merge/push: your changes are committed to "
                f"your branch automatically when you finish."
            )
        if ctx.scratchpad_dir:
            parts.append(
                f"Scratchpad directory: {ctx.scratchpad_dir}\n"
                "You can read and write files here without permission prompts."
            )
        parts.append(args.task)
        return "\n\n".join(parts)

    def _legacy_stream_event(
        self, event: ToolResultEvent, ctx: InvokeContext
    ) -> ToolStreamEvent | None:
        if not event.result or not event.tool_class:
            return None
        adapter = ToolUIDataAdapter(event.tool_class)
        display = adapter.get_result_display(event)
        return ToolStreamEvent(
            tool_name=self.get_name(),
            message=f"{event.tool_name}: {display.message}",
            tool_call_id=ctx.tool_call_id,
        )

    async def _finalize_worktree(
        self, worktree: PreparedWorktree | None, args: TaskArgs, *, completed: bool
    ) -> _WorktreeOutcome:
        if worktree is None:
            return _WorktreeOutcome()

        outcome = _WorktreeOutcome(
            branch=worktree.branch, worktree_path=str(worktree.root)
        )
        try:
            message = f"vibe {args.agent}: {args.task[:_COMMIT_SUBJECT_MAX]}"
            outcome.commit = await asyncio.to_thread(commit_worktree, worktree, message)
        except WorktreeError as e:
            outcome.note = str(e)
            return outcome

        branch_head = await asyncio.to_thread(_branch_head, worktree)
        if outcome.commit is None and worktree.base_commit == branch_head:
            outcome.merge_status = "no_changes"
        else:
            await self._report_and_merge(worktree, outcome, completed=completed)

        await self._cleanup_worktree(worktree, outcome, completed=completed)
        return outcome

    async def _report_and_merge(
        self, worktree: PreparedWorktree, outcome: _WorktreeOutcome, *, completed: bool
    ) -> None:
        try:
            report = await asyncio.to_thread(
                merge_report, worktree.repo_root, worktree.branch
            )
        except WorktreeError as e:
            outcome.note = str(e)
            return

        outcome.files_changed = list(report.files_changed)
        outcome.conflicting_paths = list(report.conflicting_paths)

        if self.config.merge != "auto" or not completed:
            return
        if not report.clean:
            outcome.merge_status = "conflicts"
            return
        try:
            await asyncio.to_thread(merge_branch, worktree.repo_root, worktree.branch)
            outcome.merge_status = "merged"
        except WorktreeError as e:
            outcome.merge_status = "conflicts"
            outcome.note = str(e)

    async def _cleanup_worktree(
        self, worktree: PreparedWorktree, outcome: _WorktreeOutcome, *, completed: bool
    ) -> None:
        keep = self.config.keep_worktrees
        should_remove = keep == "never" or (keep == "on-failure" and completed)
        if not should_remove:
            return
        delete_branch = outcome.merge_status in {"merged", "no_changes"}
        with suppress(WorktreeError):
            await asyncio.to_thread(
                remove_worktree, worktree, delete_branch=delete_branch
            )


def _apply_sandbox_overrides(config: VibeConfig, worktree: PreparedWorktree) -> None:
    sandbox_glob = str(worktree.root / "**")
    for tool_name in ("write_file", "edit"):
        tool_config = dict(config.tools.get(tool_name, {}))
        existing = tool_config.get("allowlist")
        existing_allowlist = existing if isinstance(existing, list) else []
        tool_config["permission"] = "never"
        tool_config["allowlist"] = [sandbox_glob, *existing_allowlist]
        config.tools[tool_name] = tool_config


def _branch_head(worktree: PreparedWorktree) -> str:
    from git import Repo

    return Repo(worktree.root).head.commit.hexsha


def _sanitize(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]", "", value) or uuid4().hex[:8]
