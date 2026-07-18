from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from vibe.cli.textual_ui.widgets.miou_miou_miou import (
    MiouMiouMiouAgentRow,
    MiouMiouMiouCallMessage,
    MiouMiouMiouPhaseGroup,
)
from vibe.cli.textual_ui.widgets.status_message import IndicatorState


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
        widget = MiouMiouMiouCallMessage(tool_name="miou_miou_miou")
        await app.mount(widget)
        await widget.handle_miou_miou_miou_event({
            "kind": "phase_started",
            "title": "Scan",
        })
        await widget.handle_miou_miou_miou_event(_started(1, "review:bugs", "Scan"))
        await widget.handle_miou_miou_miou_event(_started(2, "review:perf", "Scan"))
        await widget.handle_miou_miou_miou_event({
            "kind": "agent_progress",
            "agent_id": 1,
            "message": "grep: 3 matches",
        })
        await widget.handle_miou_miou_miou_event(_finished(1, "ok", duration_s=12.0))
        await widget.handle_miou_miou_miou_event(_finished(2, "error", detail="boom"))
        await pilot.pause()

        phases = list(widget.query(MiouMiouMiouPhaseGroup))
        assert len(phases) == 1
        rows = list(widget.query(MiouMiouMiouAgentRow))
        assert len(rows) == 2
        assert rows[0].get_content() == "▸ review:bugs · 12s"
        assert rows[0]._state is IndicatorState.SUCCESS
        assert rows[1].get_content() == "review:perf · boom"
        assert rows[1]._state is IndicatorState.ERROR
        assert widget.get_content_suffix() == "2/2 agents"


@pytest.mark.asyncio
async def test_logs_are_capped() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        widget = MiouMiouMiouCallMessage(tool_name="miou_miou_miou")
        await app.mount(widget)
        for i in range(5):
            await widget.handle_miou_miou_miou_event({
                "kind": "log",
                "message": f"line {i}",
            })
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
        widget = MiouMiouMiouCallMessage(tool_name="miou_miou_miou")
        await app.mount(widget)
        for i in range(1, 11):
            await widget.handle_miou_miou_miou_event(_started(i, f"agent-{i}", "Scan"))
            await widget.handle_miou_miou_miou_event(_finished(i))
        await pilot.pause()
        rows = list(widget.query(MiouMiouMiouAgentRow))
        assert len(rows) == 6
        assert widget.get_content_suffix() == "10/10 agents"


@pytest.mark.asyncio
async def test_settle_mutes_unfinished_rows() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        widget = MiouMiouMiouCallMessage(tool_name="miou_miou_miou")
        await app.mount(widget)
        await widget.handle_miou_miou_miou_event(_started(1, "still-running"))
        widget.settle(IndicatorState.MUTED)
        await pilot.pause()
        rows = list(widget.query(MiouMiouMiouAgentRow))
        assert rows[0].finished
        assert rows[0]._state is IndicatorState.MUTED


@pytest.mark.asyncio
async def test_activity_tail_visible_while_running_hidden_after() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        widget = MiouMiouMiouCallMessage(tool_name="miou_miou_miou")
        await app.mount(widget)
        await widget.handle_miou_miou_miou_event(_started(1, "deep-agent"))
        for i in range(4):
            await widget.handle_miou_miou_miou_event({
                "kind": "agent_progress",
                "agent_id": 1,
                "message": f"▸ step {i}",
            })
        await pilot.pause()
        row = list(widget.query(MiouMiouMiouAgentRow))[0]
        activity = row._activity
        assert activity is not None
        assert activity.display is True
        visible = [c for c in activity.children if c.display]
        assert len(visible) == 2
        assert str(visible[-1].render()) == "▸ step 3"
        assert row.activity_log == [f"▸ step {i}" for i in range(4)]

        await widget.handle_miou_miou_miou_event(_finished(1))
        await pilot.pause()
        assert activity.display is False


@pytest.mark.asyncio
async def test_row_click_requests_inspection() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        widget = MiouMiouMiouCallMessage(tool_name="miou_miou_miou")
        await app.mount(widget)
        await widget.handle_miou_miou_miou_event(_started(7, "clicky"))
        await pilot.pause()

        requests: list[MiouMiouMiouCallMessage.InspectRequested] = []
        original_post = widget.post_message

        def capture(message: object) -> bool:
            if isinstance(message, MiouMiouMiouCallMessage.InspectRequested):
                requests.append(message)
                return True
            return original_post(message)

        widget.post_message = capture  # type: ignore[method-assign]
        row = widget.agent_rows[7]
        widget.on_miou_miou_miou_agent_row_clicked(MiouMiouMiouAgentRow.Clicked(row))
        assert len(requests) == 1
        assert requests[0].agent_id == 7
        assert requests[0].miou_miou_miou is widget


@pytest.mark.asyncio
async def test_finished_agent_records_output() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        widget = MiouMiouMiouCallMessage(tool_name="miou_miou_miou")
        await app.mount(widget)
        await widget.handle_miou_miou_miou_event(_started(1, "worker"))
        await widget.handle_miou_miou_miou_event(
            _finished(1, "ok", output='{"summary": "all good"}')
        )
        await pilot.pause()
        assert widget.agent_rows[1].output == '{"summary": "all good"}'


@pytest.mark.asyncio
async def test_activity_history_is_capped() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        widget = MiouMiouMiouCallMessage(tool_name="miou_miou_miou")
        await app.mount(widget)
        await widget.handle_miou_miou_miou_event(_started(1, "busy-agent"))
        for i in range(60):
            await widget.handle_miou_miou_miou_event({
                "kind": "agent_progress",
                "agent_id": 1,
                "message": f"line {i}",
            })
        await pilot.pause()
        row = list(widget.query(MiouMiouMiouAgentRow))[0]
        activity = row._activity
        assert activity is not None
        assert len(activity.children) == 50
        assert str(activity.children[-1].render()) == "line 59"


@pytest.mark.asyncio
async def test_cached_agent_shows_replay_detail() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        widget = MiouMiouMiouCallMessage(tool_name="miou_miou_miou")
        await app.mount(widget)
        await widget.handle_miou_miou_miou_event(_started(1, "review:bugs"))
        await widget.handle_miou_miou_miou_event(_finished(1, "ok", cached=True))
        await pilot.pause()
        rows = list(widget.query(MiouMiouMiouAgentRow))
        assert rows[0].get_content() == "review:bugs · replayed from journal"
