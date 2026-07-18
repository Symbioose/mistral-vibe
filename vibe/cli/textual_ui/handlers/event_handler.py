from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from textual.widget import Widget

from vibe.cli.textual_ui.widgets.cats import KittenMessage, OrchestratorCatMessage
from vibe.cli.textual_ui.widgets.compact import CompactMessage
from vibe.cli.textual_ui.widgets.loading import DEFAULT_LOADING_STATUS
from vibe.cli.textual_ui.widgets.meow_meow_meow import MeowMeowMeowCallMessage
from vibe.cli.textual_ui.widgets.messages import (
    AssistantMessage,
    HookRunContainer,
    HookSystemMessageLine,
    PlanFileMessage,
    ReasoningMessage,
)
from vibe.cli.textual_ui.widgets.no_markup_static import NoMarkupStatic
from vibe.cli.textual_ui.widgets.tools import ToolCallMessage, ToolResultMessage
from vibe.core.hooks.models import (
    HookEndEvent,
    HookEvent,
    HookRunEndEvent,
    HookRunStartEvent,
    HookStartEvent,
    HookType,
)
from vibe.core.tools.ui import ToolUIDataAdapter
from vibe.core.types import (
    AgentProfileChangedEvent,
    AssistantEvent,
    BaseEvent,
    CompactEndEvent,
    CompactStartEvent,
    ContextClearedEvent,
    PlanReviewEndedEvent,
    PlanReviewRequestedEvent,
    ReasoningEvent,
    SessionTitleUpdatedEvent,
    SubagentFinishedEvent,
    SubagentStartedEvent,
    ToolCallEvent,
    ToolResultEvent,
    ToolStreamEvent,
    UserMessageEvent,
    WaitingForInputEvent,
)
from vibe.core.utils import TaggedText

if TYPE_CHECKING:
    from vibe.cli.textual_ui.widgets.loading import LoadingWidget


class EventHandler:
    def __init__(
        self,
        mount_callback: Callable,
        get_tools_collapsed: Callable[[], bool],
        on_profile_changed: Callable[[], None] | None = None,
        on_context_cleared: Callable[[Path | None], Awaitable[None]] | None = None,
    ) -> None:
        self.mount_callback = mount_callback
        self.get_tools_collapsed = get_tools_collapsed
        self.on_profile_changed = on_profile_changed
        self.on_context_cleared = on_context_cleared
        self.tool_calls: dict[str, ToolCallMessage] = {}
        self.current_compact: CompactMessage | None = None
        self.current_streaming_message: AssistantMessage | None = None
        self.current_streaming_reasoning: ReasoningMessage | None = None
        self.plan_file_message: PlanFileMessage | None = None
        # Keyed by "agent_turn", "pre_tool:{call_id}", "post_tool:{call_id}"
        self._hook_containers: dict[str, HookRunContainer] = {}
        # Per-tool-call anchor for correct widget ordering during concurrent calls.
        self._tool_call_anchors: dict[str, Widget] = {}
        # Errored results shown muted while their verdict is unknown: a follow-up
        # tool call confirms them recoverable (stay muted), turn end escalates
        # them to hard errors.
        self._pending_error_results: list[ToolResultMessage] = []
        # One kitten per running subagent, one fat cat overseeing them.
        self._kittens: dict[str, KittenMessage] = {}
        self._orchestrator_cat: OrchestratorCatMessage | None = None

    async def _handle_hook_event(
        self, event: HookEvent, loading_widget: LoadingWidget | None = None
    ) -> None:
        match event:
            case HookRunStartEvent():
                await self._handle_hook_run_start(event)
            case HookRunEndEvent():
                await self._handle_hook_run_end(event)
            case HookStartEvent():
                await self.finalize_streaming()
                if loading_widget:
                    loading_widget.set_status(f"Running hook {event.hook_name}")
            case HookEndEvent():
                if event.content:
                    key = self._hook_container_key(event.scope, event.tool_call_id)
                    container = self._hook_containers.get(key)
                    if container is not None:
                        widget = HookSystemMessageLine(
                            hook_name=event.hook_name,
                            content=event.content,
                            severity=event.status,
                        )
                        await container.add_message(widget)
                        if event.scope == HookType.PRE_TOOL and event.tool_call_id:
                            tool_call = self.tool_calls.get(event.tool_call_id)
                            if tool_call is not None:
                                tool_call.add_class("no-gap")
                if loading_widget:
                    loading_widget.set_status(DEFAULT_LOADING_STATUS)

    @staticmethod
    def _hook_container_key(scope: HookType, tool_call_id: str | None) -> str:
        if scope == HookType.POST_AGENT:
            return "agent_turn"
        return f"{scope.value}:{tool_call_id or ''}"

    async def _handle_hook_run_start(self, event: HookRunStartEvent) -> None:
        container = HookRunContainer()
        key = self._hook_container_key(event.scope, event.tool_call_id)
        self._hook_containers[key] = container

        if event.scope == HookType.PRE_TOOL:
            container.add_class("hook-pre-tool")

        anchor = self._tool_call_anchors.get(event.tool_call_id or "")
        if event.scope == HookType.PRE_TOOL and anchor is not None:
            # Mount *above* the tool call widget so it reads as a precondition.
            await self.mount_callback(container, before=anchor)
        elif event.scope == HookType.POST_TOOL and anchor is not None:
            await self.mount_callback(container, after=anchor)
        else:
            await self.mount_callback(container)

    async def _handle_hook_run_end(self, event: HookRunEndEvent) -> None:
        key = self._hook_container_key(event.scope, event.tool_call_id)
        container = self._hook_containers.pop(key, None)
        if container is None:
            return
        if container.display:
            # PRE_TOOL containers mount *above* the call widget, so they
            # must not become the anchor — the result still needs to land
            # after the call widget, not after the hook container.
            if event.scope != HookType.PRE_TOOL and event.tool_call_id:
                self._tool_call_anchors[event.tool_call_id] = container
        else:
            await container.remove()

    async def handle_event(  # noqa: PLR0912
        self, event: BaseEvent, loading_widget: LoadingWidget | None = None
    ) -> ToolCallMessage | None:
        if event.agent is not None:
            await self._handle_subagent_event(event)
            return None
        match event:
            case ReasoningEvent():
                await self._handle_reasoning_message(event)
            case AssistantEvent():
                await self._handle_assistant_message(event)
            case ToolCallEvent():
                await self.finalize_streaming()
                return await self._handle_tool_call(event, loading_widget)
            case ToolResultEvent():
                await self.finalize_streaming()
                sanitized_event = self._sanitize_event(event)
                await self._handle_tool_result(sanitized_event)
            case ToolStreamEvent():
                await self._handle_tool_stream(event)
            case CompactStartEvent():
                await self.finalize_streaming()
                await self._handle_compact_start()
            case CompactEndEvent():
                await self.finalize_streaming()
                await self._handle_compact_end(event)
            case AgentProfileChangedEvent():
                if self.on_profile_changed:
                    self.on_profile_changed()
            case ContextClearedEvent():
                await self.finalize_streaming()
                if self.on_context_cleared:
                    await self.on_context_cleared(event.plan_file_path)
            case SessionTitleUpdatedEvent():
                pass
            case UserMessageEvent():
                await self.finalize_streaming()
            case HookEvent():
                await self._handle_hook_event(event, loading_widget)
            case PlanReviewRequestedEvent():
                await self._handle_start_plan_review(file_path=event.file_path)
            case PlanReviewEndedEvent():
                self._handle_stop_plan_review()
            case WaitingForInputEvent():
                await self.finalize_streaming()
            case _:
                await self.finalize_streaming()
                await self._handle_unknown_event(event)
        return None

    def _sanitize_event(self, event: ToolResultEvent) -> ToolResultEvent:
        if isinstance(event, ToolResultEvent):
            return ToolResultEvent(
                tool_name=event.tool_name,
                tool_class=event.tool_class,
                result=event.result,
                error=TaggedText.from_string(event.error).message
                if event.error
                else None,
                skipped=event.skipped,
                skip_reason=TaggedText.from_string(event.skip_reason).message
                if event.skip_reason
                else None,
                cancelled=event.cancelled,
                duration=event.duration,
                tool_call_id=event.tool_call_id,
            )
        return event

    async def _handle_tool_call(
        self, event: ToolCallEvent, loading_widget: LoadingWidget | None = None
    ) -> ToolCallMessage | None:
        tool_call_id = event.tool_call_id
        existing_tool_call = self.tool_calls.get(tool_call_id) if tool_call_id else None
        if existing_tool_call:
            existing_tool_call.update_event(event)
            tool_call = existing_tool_call
        else:
            # A follow-up tool call is the recovery signal: leave prior errors muted.
            self._resolve_pending_errors(escalate=False)
            if event.tool_name == "meow_meow_meow":
                tool_call = MeowMeowMeowCallMessage(event)
            else:
                tool_call = ToolCallMessage(event)
            if tool_call_id:
                self.tool_calls[tool_call_id] = tool_call
                self._tool_call_anchors[tool_call_id] = tool_call
            await self.mount_callback(tool_call)

        if loading_widget and event.tool_class:
            adapter = ToolUIDataAdapter(event.tool_class)
            loading_widget.set_status(adapter.get_status_text())

        return tool_call

    async def _handle_tool_result(self, event: ToolResultEvent) -> None:
        tool_call_id = event.tool_call_id
        call_widget = self.tool_calls.get(tool_call_id) if tool_call_id else None
        anchor = (
            self._tool_call_anchors.get(tool_call_id) if tool_call_id else None
        ) or call_widget

        tool_result = ToolResultMessage(event, call_widget)
        await self.mount_callback(tool_result, after=anchor)

        if event.error and not event.skipped and not event.cancelled:
            self._pending_error_results.append(tool_result)

        if tool_call_id:
            self._tool_call_anchors[tool_call_id] = tool_result
            if tool_call_id in self.tool_calls:
                del self.tool_calls[tool_call_id]

    def _resolve_pending_errors(self, *, escalate: bool) -> None:
        if escalate:
            for result in self._pending_error_results:
                result.escalate_error()
        self._pending_error_results.clear()

    def escalate_unresolved_errors(self) -> None:
        # Called at turn end: errors not followed by a tool call were terminal.
        self._resolve_pending_errors(escalate=True)

    async def _handle_tool_stream(self, event: ToolStreamEvent) -> None:
        tool_call = self.tool_calls.get(event.tool_call_id)
        if tool_call is None:
            return
        if isinstance(tool_call, MeowMeowMeowCallMessage) and event.data is not None:
            await tool_call.handle_meow_meow_meow_event(event.data)
        else:
            tool_call.set_stream_message(event.message)

    async def _handle_subagent_event(self, event: BaseEvent) -> None:
        # Attributed child events stay off the main transcript: the task tool
        # already streams a per-tool summary. Only lifecycle milestones are
        # surfaced — as cats, obviously.
        if event.agent is None:
            return
        match event:
            case SubagentStartedEvent():
                await self._mount_kitten(event)
            case SubagentFinishedEvent():
                status = event.status
                if event.merge_status != "not_attempted":
                    status += f" ({event.merge_status.replace('_', ' ')})"
                if kitten := self._kittens.pop(event.tool_call_id, None):
                    kitten.set_status(status)
                elif event.agent is not None:
                    self._set_subagent_stream(
                        event.tool_call_id, f"{event.agent.name}: {status}"
                    )
            case _:
                pass

    async def _mount_kitten(self, event: SubagentStartedEvent) -> None:
        if event.agent is None:
            return
        anchor = self._tool_call_anchors.get(event.tool_call_id)
        if anchor is None:
            message = f"{event.agent.name}: started"
            if event.branch:
                message += f" on branch {event.branch}"
            self._set_subagent_stream(event.tool_call_id, message)
            return

        if self._orchestrator_cat is None:
            # Parallel workers start in any order: pin the fat cat above the
            # first pending tool-call widget so it heads the whole group.
            first_call_widget = next(iter(self.tool_calls.values()), None)
            self._orchestrator_cat = OrchestratorCatMessage()
            await self.mount_callback(
                self._orchestrator_cat, before=first_call_widget or anchor
            )
        self._orchestrator_cat.add_kitten()

        kitten = KittenMessage(event.agent.name, branch=event.branch)
        self._kittens[event.tool_call_id] = kitten
        await self.mount_callback(kitten, after=anchor)
        self._tool_call_anchors[event.tool_call_id] = kitten

    def _set_subagent_stream(self, tool_call_id: str, message: str) -> None:
        if tool_call := self.tool_calls.get(tool_call_id):
            tool_call.set_stream_message(message)

    async def _handle_assistant_message(self, event: AssistantEvent) -> None:
        if self.current_streaming_reasoning is not None:
            self.current_streaming_reasoning.stop_spinning()
            await self.current_streaming_reasoning.stop_stream()
            self.current_streaming_reasoning = None

        if self.current_streaming_message is None:
            msg = AssistantMessage(event.content)
            self.current_streaming_message = msg
            await self.mount_callback(msg)
        else:
            await self.current_streaming_message.append_content(event.content)

    async def _handle_reasoning_message(self, event: ReasoningEvent) -> None:
        if self.current_streaming_message is not None:
            await self.current_streaming_message.stop_stream()
            if self.current_streaming_message.is_stripped_content_empty():
                await self.current_streaming_message.remove()
            self.current_streaming_message = None

        if self.current_streaming_reasoning is None:
            tools_collapsed = self.get_tools_collapsed()
            msg = ReasoningMessage(event.content, collapsed=tools_collapsed)
            self.current_streaming_reasoning = msg
            await self.mount_callback(msg)
        else:
            await self.current_streaming_reasoning.append_content(event.content)

    async def _handle_compact_start(self) -> None:
        compact_msg = CompactMessage()
        self.current_compact = compact_msg
        await self.mount_callback(compact_msg)

    async def _handle_compact_end(self, event: CompactEndEvent) -> None:
        if self.current_compact:
            self.current_compact.set_complete(
                old_session_id=event.old_session_id, new_session_id=event.new_session_id
            )
            self.current_compact = None

    async def _handle_unknown_event(self, event: BaseEvent) -> None:
        await self.mount_callback(NoMarkupStatic(str(event), classes="unknown-event"))

    async def finalize_streaming(self) -> None:
        if self.current_streaming_reasoning is not None:
            self.current_streaming_reasoning.stop_spinning()
            await self.current_streaming_reasoning.stop_stream()
            self.current_streaming_reasoning = None
        if self.current_streaming_message is not None:
            await self.current_streaming_message.stop_stream()
            self.current_streaming_message = None

    def stop_current_tool_call(
        self, success: bool = True, *, cancelled: bool = False
    ) -> None:
        for tool_call in self.tool_calls.values():
            if cancelled:
                # A user interrupt is neutral, not a failure: hold a grey square.
                tool_call.show_muted()
            else:
                tool_call.stop_spinning(success=success)
        self.tool_calls.clear()
        self._tool_call_anchors.clear()
        self._hook_containers.clear()
        self._kittens.clear()
        self._orchestrator_cat = None
        # On cancel nothing is terminal -- leave prior errors muted too.
        self._resolve_pending_errors(escalate=not cancelled)

    def stop_current_compact(self) -> None:
        if self.current_compact:
            self.current_compact.stop_spinning(success=False)
            self.current_compact = None

    async def _handle_start_plan_review(self, file_path: Path) -> None:
        file_path.touch()
        msg = PlanFileMessage(file_path=file_path)
        self.plan_file_message = msg
        await self.mount_callback(msg)

    def _handle_stop_plan_review(self) -> None:
        if self.plan_file_message is None:
            return

        self.plan_file_message.stop_watching()
        self.plan_file_message = None
