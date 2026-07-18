from __future__ import annotations

from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical

from vibe.cli.textual_ui.widgets.links import LinkStatic
from vibe.cli.textual_ui.widgets.no_markup_static import (
    NoMarkupStatic,
    NonSelectableStatic,
)
from vibe.cli.textual_ui.widgets.status_message import IndicatorState, StatusMessage
from vibe.cli.textual_ui.widgets.tools import ToolCallMessage
from vibe.core.workflows.models import AgentRunStatus

_MAX_LOG_LINES = 3
_KEEP_FINISHED_ROWS_PER_PHASE = 6
_PROGRESS_MAX_LEN = 48


class WorkflowAgentRow(StatusMessage):
    def __init__(self, label: str) -> None:
        self._label = label
        self._detail = ""
        super().__init__()
        self.add_class("workflow-agent-row")
        self.finished = False

    def get_content(self) -> str:
        if self._detail:
            return f"{self._label} · {self._detail}"
        return self._label

    def set_progress(self, message: str) -> None:
        if len(message) > _PROGRESS_MAX_LEN:
            message = message[: _PROGRESS_MAX_LEN - 1] + "…"
        self._detail = message
        self.update_display()

    def finish(
        self,
        status: str,
        *,
        cached: bool = False,
        duration_s: float | None = None,
        detail: str | None = None,
    ) -> None:
        self.finished = True
        if cached:
            self._detail = "replayed from journal"
        elif status == AgentRunStatus.ERROR and detail:
            self._detail = (
                detail
                if len(detail) <= _PROGRESS_MAX_LEN
                else detail[: _PROGRESS_MAX_LEN - 1] + "…"
            )
        elif duration_s is not None and duration_s >= 1:
            self._detail = f"{duration_s:.0f}s"
        else:
            self._detail = ""
        match status:
            case AgentRunStatus.OK:
                self.settle(IndicatorState.SUCCESS)
            case AgentRunStatus.ERROR:
                self.settle(IndicatorState.ERROR)
            case _:
                self.settle(IndicatorState.MUTED)


class WorkflowPhaseGroup(Vertical):
    def __init__(self, title: str | None) -> None:
        self._title = title
        self._header: NoMarkupStatic | None = None
        self._rows: Vertical | None = None
        self._pruned = 0
        super().__init__()
        self.add_class("workflow-phase")

    def compose(self) -> ComposeResult:
        if self._title is not None:
            self._header = NoMarkupStatic(
                f"◆ {self._title}", classes="workflow-phase-header"
            )
            yield self._header
        self._rows = Vertical(classes="workflow-phase-rows")
        yield self._rows

    async def add_row(self, row: WorkflowAgentRow) -> None:
        if self._rows is not None:
            await self._rows.mount(row)

    async def prune_finished(self) -> None:
        if self._rows is None:
            return
        finished = [
            child
            for child in self._rows.children
            if isinstance(child, WorkflowAgentRow) and child.finished
        ]
        overflow = len(finished) - _KEEP_FINISHED_ROWS_PER_PHASE
        if overflow <= 0:
            return
        for row in finished[:overflow]:
            self._pruned += 1
            await row.remove()
        if self._pruned and self._header is not None and self._title is not None:
            self._header.update(f"◆ {self._title} (+{self._pruned} earlier)")


class WorkflowCallMessage(ToolCallMessage):
    def __init__(self, event: Any = None, **kwargs: Any) -> None:
        self._tree: Vertical | None = None
        self._logs: Vertical | None = None
        self._phases: dict[str | None, WorkflowPhaseGroup] = {}
        self._agents: dict[int, WorkflowAgentRow] = {}
        self._agent_phase: dict[int, str | None] = {}
        self._agents_total = 0
        self._agents_finished = 0
        super().__init__(event, **kwargs)
        self.add_class("workflow-call")

    def compose(self) -> ComposeResult:
        with Vertical(classes="tool-call-container"):
            with Horizontal(classes="tool-call-header"):
                self._indicator_widget = NonSelectableStatic(
                    self._spinner.current_frame(), classes="status-indicator-icon"
                )
                yield self._indicator_widget
                self._text_widget = LinkStatic("", classes="status-indicator-text")
                yield self._text_widget
                self._suffix_widget = NoMarkupStatic(
                    "", classes="status-indicator-suffix"
                )
                self._suffix_widget.display = False
                yield self._suffix_widget
            self._tree = Vertical(classes="workflow-tree")
            yield self._tree
            self._logs = Vertical(classes="workflow-logs")
            self._logs.display = False
            yield self._logs
            self._stream_widget = NoMarkupStatic("", classes="tool-stream-message")
            self._stream_widget.display = False
            yield self._stream_widget

    def get_content_suffix(self) -> str:
        if not self._agents_total:
            return ""
        return f"{self._agents_finished}/{self._agents_total} agents"

    def settle(self, state: IndicatorState) -> None:
        for row in self._agents.values():
            if not row.finished:
                row.finish(AgentRunStatus.CANCELLED)
        super().settle(state)

    async def handle_workflow_event(self, data: dict[str, Any]) -> None:
        match data.get("kind"):
            case "phase_started":
                await self._ensure_phase(data["title"])
            case "agent_started":
                await self._on_agent_started(data)
            case "agent_progress":
                row = self._agents.get(data["agent_id"])
                if row is not None and not row.finished:
                    row.set_progress(data["message"])
            case "agent_finished":
                await self._on_agent_finished(data)
            case "log":
                await self._append_log(data["message"])
            case _:
                pass

    async def _ensure_phase(self, title: str | None) -> WorkflowPhaseGroup:
        group = self._phases.get(title)
        if group is None:
            group = WorkflowPhaseGroup(title)
            self._phases[title] = group
            if self._tree is not None:
                await self._tree.mount(group)
        return group

    async def _on_agent_started(self, data: dict[str, Any]) -> None:
        agent_id = data["agent_id"]
        phase = data.get("phase")
        group = await self._ensure_phase(phase)
        row = WorkflowAgentRow(data["label"])
        self._agents[agent_id] = row
        self._agent_phase[agent_id] = phase
        self._agents_total += 1
        await group.add_row(row)
        self.update_display()

    async def _on_agent_finished(self, data: dict[str, Any]) -> None:
        agent_id = data["agent_id"]
        row = self._agents.get(agent_id)
        if row is None:
            return
        self._agents_finished += 1
        row.finish(
            data.get("status", AgentRunStatus.OK),
            cached=data.get("cached", False),
            duration_s=data.get("duration_s"),
            detail=data.get("detail"),
        )
        group = self._phases.get(self._agent_phase.get(agent_id))
        if group is not None:
            await group.prune_finished()
        self.update_display()

    async def _append_log(self, message: str) -> None:
        if self._logs is None:
            return
        self._logs.display = True
        await self._logs.mount(
            NoMarkupStatic(f"→ {message}", classes="workflow-log-line")
        )
        children = list(self._logs.children)
        for extra in children[:-_MAX_LOG_LINES]:
            await extra.remove()
