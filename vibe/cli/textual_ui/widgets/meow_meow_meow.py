from __future__ import annotations

import time
from typing import Any, ClassVar, Literal

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message

from vibe.cli.textual_ui.widgets.cats import KITTEN_ART
from vibe.cli.textual_ui.widgets.collapsible import ClickWithoutDragMixin
from vibe.cli.textual_ui.widgets.links import LinkStatic
from vibe.cli.textual_ui.widgets.no_markup_static import (
    NoMarkupStatic,
    NonSelectableStatic,
)
from vibe.cli.textual_ui.widgets.status_message import IndicatorState, StatusMessage
from vibe.cli.textual_ui.widgets.tools import ToolCallMessage
from vibe.core.meowmeowmeow.models import AgentRunStatus

_MAX_LOG_LINES = 3
_KEEP_FINISHED_ROWS_PER_PHASE = 6
_PROGRESS_MAX_LEN = 48
_MAX_ACTIVITY_LINES = 50
_MAX_ACTIVITY_LOG = 500
_RUNNING_ACTIVITY_TAIL = 2
_PHASE_DETAIL_MAX_LEN = 44

PhaseState = Literal["pending", "running", "done"]

_PHASE_ICONS: dict[PhaseState, str] = {"pending": "○", "running": "◆", "done": "✓"}


class MeowMeowMeowAgentRow(ClickWithoutDragMixin, StatusMessage):
    class Clicked(Message):
        def __init__(self, row: MeowMeowMeowAgentRow) -> None:
            super().__init__()
            self.row = row

    def __init__(
        self, label: str, *, prompt: str | None = None, phase: str | None = None
    ) -> None:
        self._label = label
        self._detail = "thinking…"
        self._activity: Vertical | None = None
        self._activity_count = 0
        self._started_at = time.monotonic()
        super().__init__()
        self.add_class("meow_meow_meow-agent-row")
        self.finished = False
        self.prompt = prompt
        self.phase_title = phase
        self.status_detail: str | None = None
        self.output: str | None = None
        self.activity_log: list[str] = []

    def compose(self) -> ComposeResult:
        with Horizontal(classes="meow_meow_meow-agent-header"):
            self._indicator_widget = NonSelectableStatic(
                self._spinner.current_frame(), classes="status-indicator-icon"
            )
            yield self._indicator_widget
            self._text_widget = NoMarkupStatic("", classes="status-indicator-text")
            yield self._text_widget
        self._activity = Vertical(classes="meow_meow_meow-agent-activity")
        self._activity.display = False
        yield self._activity

    def get_content(self) -> str:
        marker = "▸ " if self._activity_count else ""
        if not self.finished:
            elapsed = f"{time.monotonic() - self._started_at:.0f}s"
            if self._detail:
                return f"{marker}{self._label} · {elapsed} · {self._detail}"
            return f"{marker}{self._label} · {elapsed}"
        if self._detail:
            return f"{marker}{self._label} · {self._detail}"
        return f"{marker}{self._label}"

    async def add_activity(self, message: str) -> None:
        self.activity_log.append(message)
        if len(self.activity_log) > _MAX_ACTIVITY_LOG:
            self.activity_log.pop(0)
        self._detail = (
            message
            if len(message) <= _PROGRESS_MAX_LEN
            else message[: _PROGRESS_MAX_LEN - 1] + "…"
        )
        if self._activity is not None:
            await self._activity.mount(
                NoMarkupStatic(message, classes="meow_meow_meow-activity-line")
            )
            self._activity_count += 1
            children = list(self._activity.children)
            for extra in children[:-_MAX_ACTIVITY_LINES]:
                self._activity_count -= 1
                await extra.remove()
        self._refresh_activity()
        self.update_display()

    def _refresh_activity(self) -> None:
        if self._activity is None:
            return
        children = list(self._activity.children)
        visible = min(_RUNNING_ACTIVITY_TAIL, len(children)) if not self.finished else 0
        for index, child in enumerate(children):
            child.display = index >= len(children) - visible
        self._activity.display = visible > 0

    async def on_click(self, event: events.Click) -> None:
        if self._click_is_passive(event):
            return
        event.stop()
        self.post_message(self.Clicked(self))

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
        self.status_detail = detail
        match status:
            case AgentRunStatus.OK:
                self.settle(IndicatorState.SUCCESS)
            case AgentRunStatus.ERROR:
                self.settle(IndicatorState.ERROR)
            case _:
                self.settle(IndicatorState.MUTED)
        self._refresh_activity()


class MeowMeowMeowPhaseGroup(Vertical):
    def __init__(self, title: str | None, detail: str | None = None) -> None:
        self._title = title
        self._detail = detail
        self._header: NoMarkupStatic | None = None
        self._rows: Vertical | None = None
        self._pruned = 0
        super().__init__()
        self.add_class("meow_meow_meow-phase")
        self.state: PhaseState = "pending"
        self.started_count = 0
        self.finished_count = 0

    def compose(self) -> ComposeResult:
        if self._title is not None:
            self._header = NoMarkupStatic("", classes="meow_meow_meow-phase-header")
            yield self._header
        self._rows = Vertical(classes="meow_meow_meow-phase-rows")
        yield self._rows
        self.refresh_header()

    def set_state(self, state: PhaseState) -> None:
        self.state = state
        self.refresh_header()

    def refresh_header(self) -> None:
        if self._header is None:
            return
        parts = [f"{_PHASE_ICONS[self.state]} {self._title}"]
        running = self.started_count - self.finished_count
        if running > 0:
            parts.append(f"{running} running")
        if self.finished_count:
            parts.append(f"{self.finished_count}/{self.started_count} done")
        if self._pruned:
            parts.append(f"+{self._pruned} folded")
        if self.state == "pending" and self._detail:
            detail = self._detail
            if len(detail) > _PHASE_DETAIL_MAX_LEN:
                detail = detail[: _PHASE_DETAIL_MAX_LEN - 1] + "…"
            parts.append(detail)
        self._header.update(" · ".join(parts))
        for state in ("pending", "running", "done"):
            self._header.set_class(state == self.state, state)

    async def add_row(self, row: MeowMeowMeowAgentRow) -> None:
        if self._rows is not None:
            await self._rows.mount(row)

    async def prune_finished(self) -> None:
        if self._rows is None:
            return
        finished = [
            child
            for child in self._rows.children
            if isinstance(child, MeowMeowMeowAgentRow) and child.finished
        ]
        overflow = len(finished) - _KEEP_FINISHED_ROWS_PER_PHASE
        if overflow <= 0:
            return
        for row in finished[:overflow]:
            self._pruned += 1
            await row.remove()
        self.refresh_header()


class MeowMeowMeowCallMessage(ToolCallMessage):
    class InspectRequested(Message):
        def __init__(
            self, meow_meow_meow: MeowMeowMeowCallMessage, agent_id: int | None = None
        ) -> None:
            super().__init__()
            self.meow_meow_meow = meow_meow_meow
            self.agent_id = agent_id

    instances: ClassVar[list[MeowMeowMeowCallMessage]] = []

    def __init__(self, event: Any = None, **kwargs: Any) -> None:
        self._tree: Vertical | None = None
        self._logs: Vertical | None = None
        self._cat_row: Horizontal | None = None
        self._cat_label: NoMarkupStatic | None = None
        self._phases: dict[str | None, MeowMeowMeowPhaseGroup] = {}
        self._agents: dict[int, MeowMeowMeowAgentRow] = {}
        self._agent_phase: dict[int, str | None] = {}
        self._agents_total = 0
        self._agents_finished = 0
        self._current_phase: str | None = None
        super().__init__(event, **kwargs)
        self.add_class("meow_meow_meow-call")
        MeowMeowMeowCallMessage.instances.append(self)

    @property
    def agent_rows(self) -> dict[int, MeowMeowMeowAgentRow]:
        return self._agents

    @property
    def phase_order(self) -> list[str | None]:
        return list(self._phases.keys())

    def on_meow_meow_meow_agent_row_clicked(
        self, message: MeowMeowMeowAgentRow.Clicked
    ) -> None:
        agent_id = next(
            (i for i, row in self._agents.items() if row is message.row), None
        )
        self.post_message(self.InspectRequested(self, agent_id))

    async def on_click(self, _event: events.Click) -> None:
        self.post_message(self.InspectRequested(self, None))

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
            self._cat_row = Horizontal(classes="cat-container meow-cat")
            self._cat_row.display = False
            with self._cat_row:
                yield NonSelectableStatic(KITTEN_ART, classes="cat-art kitten-art")
                self._cat_label = NoMarkupStatic("", classes="cat-label kitten-label")
                yield self._cat_label
            yield self._cat_row
            self._tree = Vertical(classes="meow_meow_meow-tree")
            yield self._tree
            self._logs = Vertical(classes="meow_meow_meow-logs")
            self._logs.display = False
            yield self._logs
            self._stream_widget = NoMarkupStatic("", classes="tool-stream-message")
            self._stream_widget.display = False
            yield self._stream_widget

    def get_content_suffix(self) -> str:
        if not self._agents_total:
            return ""
        counts = f"{self._agents_finished}/{self._agents_total} agents"
        if self._is_spinning:
            return f"{counts} · ctrl+w inspect"
        return counts

    def settle(self, state: IndicatorState) -> None:
        for row in self._agents.values():
            if not row.finished:
                row.finish(AgentRunStatus.CANCELLED)
        for group in self._phases.values():
            group.finished_count = group.started_count
            if group.state == "running":
                group.set_state("done")
            else:
                group.refresh_header()
        super().settle(state)
        self._refresh_cat()

    def _refresh_cat(self) -> None:
        if self._cat_row is None or self._cat_label is None:
            return
        if not self._agents_total:
            self._cat_row.display = False
            return
        self._cat_row.display = True
        running = self._agents_total - self._agents_finished
        if self._is_spinning and running > 0:
            noun = "kitten" if running == 1 else "kittens"
            label = (
                f"meow · {running} {noun} hunting · "
                f"{self._agents_finished}/{self._agents_total}"
            )
        else:
            noun = "kitten" if self._agents_total == 1 else "kittens"
            label = f"meow · {self._agents_total} {noun} · done"
        self._cat_label.update(label)

    async def handle_meow_meow_meow_event(self, data: dict[str, Any]) -> None:
        match data.get("kind"):
            case "phases_planned":
                for planned in data.get("phases", []):
                    await self._ensure_phase(
                        planned.get("title"), detail=planned.get("detail")
                    )
            case "phase_started":
                await self._on_phase_started(data["title"])
            case "agent_started":
                await self._on_agent_started(data)
            case "agent_progress":
                row = self._agents.get(data["agent_id"])
                if row is not None and not row.finished:
                    await row.add_activity(data["message"])
            case "agent_finished":
                await self._on_agent_finished(data)
            case "log":
                await self._append_log(data["message"])
            case _:
                pass

    async def _ensure_phase(
        self, title: str | None, detail: str | None = None
    ) -> MeowMeowMeowPhaseGroup:
        group = self._phases.get(title)
        if group is None:
            group = MeowMeowMeowPhaseGroup(title, detail)
            self._phases[title] = group
            if self._tree is not None:
                await self._tree.mount(group)
        return group

    async def _on_phase_started(self, title: str) -> None:
        group = await self._ensure_phase(title)
        group.set_state("running")
        self._current_phase = title
        self._settle_idle_phases()

    def _settle_idle_phases(self) -> None:
        for phase_title, group in self._phases.items():
            if (
                phase_title != self._current_phase
                and group.state == "running"
                and group.started_count > 0
                and group.finished_count >= group.started_count
            ):
                group.set_state("done")

    async def _on_agent_started(self, data: dict[str, Any]) -> None:
        agent_id = data["agent_id"]
        phase = data.get("phase")
        group = await self._ensure_phase(phase)
        if group.state == "pending":
            group.set_state("running")
        row = MeowMeowMeowAgentRow(
            data["label"], prompt=data.get("prompt"), phase=phase
        )
        self._agents[agent_id] = row
        self._agent_phase[agent_id] = phase
        self._agents_total += 1
        group.started_count += 1
        await group.add_row(row)
        group.refresh_header()
        self._refresh_cat()
        self.update_display()

    async def _on_agent_finished(self, data: dict[str, Any]) -> None:
        agent_id = data["agent_id"]
        row = self._agents.get(agent_id)
        if row is None:
            return
        self._agents_finished += 1
        row.output = data.get("output")
        row.finish(
            data.get("status", AgentRunStatus.OK),
            cached=data.get("cached", False),
            duration_s=data.get("duration_s"),
            detail=data.get("detail"),
        )
        group = self._phases.get(self._agent_phase.get(agent_id))
        if group is not None:
            group.finished_count += 1
            await group.prune_finished()
            group.refresh_header()
        self._settle_idle_phases()
        self._refresh_cat()
        self.update_display()

    async def _append_log(self, message: str) -> None:
        if self._logs is None:
            return
        self._logs.display = True
        await self._logs.mount(
            NoMarkupStatic(f"→ {message}", classes="meow_meow_meow-log-line")
        )
        children = list(self._logs.children)
        for extra in children[:-_MAX_LOG_LINES]:
            await extra.remove()
