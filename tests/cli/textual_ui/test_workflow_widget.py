from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from vibe.cli.textual_ui.widgets.status_message import IndicatorState
from vibe.cli.textual_ui.widgets.workflow import (
    WorkflowAgentRow,
    WorkflowCallMessage,
    WorkflowPhaseGroup,
)


class _Harness(App[None]):
    def compose(self) -> ComposeResult:
        yield from ()


def _started(agent_id: int, label: str, phase: str | None = None) -> dict:
    return {
        "kind": "agent_started",
        "agent_id": agent_id,
        "label": label,
        "phase": phase,
        "cached": False,
    }


def _finished(agent_id: int, status: str = "ok", **extra: object) -> dict:
    return {"kind": "agent_finished", "agent_id": agent_id, "status": status, **extra}


@pytest.mark.asyncio
async def test_tree_builds_phases_and_rows() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        widget = WorkflowCallMessage(tool_name="workflow")
        await app.mount(widget)
        await widget.handle_workflow_event({"kind": "phase_started", "title": "Scan"})
        await widget.handle_workflow_event(_started(1, "review:bugs", "Scan"))
        await widget.handle_workflow_event(_started(2, "review:perf", "Scan"))
        await widget.handle_workflow_event({
            "kind": "agent_progress",
            "agent_id": 1,
            "message": "grep: 3 matches",
        })
        await widget.handle_workflow_event(_finished(1, "ok", duration_s=12.0))
        await widget.handle_workflow_event(_finished(2, "error", detail="boom"))
        await pilot.pause()

        phases = list(widget.query(WorkflowPhaseGroup))
        assert len(phases) == 1
        rows = list(widget.query(WorkflowAgentRow))
        assert len(rows) == 2
        assert rows[0].get_content() == "review:bugs · 12s"
        assert rows[0]._state is IndicatorState.SUCCESS
        assert rows[1].get_content() == "review:perf · boom"
        assert rows[1]._state is IndicatorState.ERROR
        assert widget.get_content_suffix() == "2/2 agents"


@pytest.mark.asyncio
async def test_logs_are_capped() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        widget = WorkflowCallMessage(tool_name="workflow")
        await app.mount(widget)
        for i in range(5):
            await widget.handle_workflow_event({"kind": "log", "message": f"line {i}"})
        await pilot.pause()
        logs = widget._logs
        assert logs is not None
        texts = [str(child.render()) for child in logs.children]
        assert len(texts) == 3
        assert texts[-1] == "→ line 4"


@pytest.mark.asyncio
async def test_finished_rows_are_pruned() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        widget = WorkflowCallMessage(tool_name="workflow")
        await app.mount(widget)
        for i in range(1, 11):
            await widget.handle_workflow_event(_started(i, f"agent-{i}", "Scan"))
            await widget.handle_workflow_event(_finished(i))
        await pilot.pause()
        rows = list(widget.query(WorkflowAgentRow))
        assert len(rows) == 6
        assert widget.get_content_suffix() == "10/10 agents"


@pytest.mark.asyncio
async def test_settle_mutes_unfinished_rows() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        widget = WorkflowCallMessage(tool_name="workflow")
        await app.mount(widget)
        await widget.handle_workflow_event(_started(1, "still-running"))
        widget.settle(IndicatorState.MUTED)
        await pilot.pause()
        rows = list(widget.query(WorkflowAgentRow))
        assert rows[0].finished
        assert rows[0]._state is IndicatorState.MUTED


@pytest.mark.asyncio
async def test_cached_agent_shows_replay_detail() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        widget = WorkflowCallMessage(tool_name="workflow")
        await app.mount(widget)
        await widget.handle_workflow_event(_started(1, "review:bugs"))
        await widget.handle_workflow_event(_finished(1, "ok", cached=True))
        await pilot.pause()
        rows = list(widget.query(WorkflowAgentRow))
        assert rows[0].get_content() == "review:bugs · replayed from journal"
