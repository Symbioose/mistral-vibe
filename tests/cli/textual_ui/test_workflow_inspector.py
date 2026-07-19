from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Tree

from vibe.cli.textual_ui.widgets.no_markup_static import NoMarkupStatic
from vibe.cli.textual_ui.widgets.workflow import WorkflowAgentRow, WorkflowCallMessage
from vibe.cli.textual_ui.widgets.workflow_inspector import WorkflowInspectorScreen


class _Harness(App[None]):
    def compose(self) -> ComposeResult:
        yield from ()


async def _make_workflow(app: _Harness) -> WorkflowCallMessage:
    widget = WorkflowCallMessage(tool_name="workflow")
    await app.mount(widget)
    await widget.handle_workflow_event({"kind": "phase_started", "title": "Scan"})
    await widget.handle_workflow_event({
        "kind": "agent_started",
        "agent_id": 1,
        "label": "scan:core",
        "phase": "Scan",
        "cached": False,
        "prompt": "Analyse vibe/core in depth and report the architecture.",
    })
    await widget.handle_workflow_event({
        "kind": "agent_progress",
        "agent_id": 1,
        "message": "▸ Reading vibe/core/agent_loop",
    })
    await widget.handle_workflow_event({
        "kind": "agent_started",
        "agent_id": 2,
        "label": "scan:cli",
        "phase": "Scan",
        "cached": False,
        "prompt": "Analyse vibe/cli in depth.",
    })
    await widget.handle_workflow_event({
        "kind": "agent_finished",
        "agent_id": 2,
        "status": "ok",
        "duration_s": 5.0,
        "output": '{"modules": ["textual_ui"]}',
    })
    return widget


@pytest.mark.asyncio
async def test_inspector_builds_tree_and_detail() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        widget = await _make_workflow(app)
        screen = WorkflowInspectorScreen(widget)
        await app.push_screen(screen)
        await pilot.pause()

        tree = screen.query_one(Tree)
        agent_nodes = screen._agent_nodes
        assert set(agent_nodes) == {1, 2}
        assert "scan:core" in str(agent_nodes[1].label)
        assert str(agent_nodes[2].label).startswith("✓")

        tree.select_node(agent_nodes[1])
        await pilot.pause()
        detail_text = " ".join(str(w.render()) for w in screen.query(NoMarkupStatic))
        assert "Analyse vibe/core in depth" in detail_text
        assert "Reading vibe/core/agent_loop" in detail_text


@pytest.mark.asyncio
async def test_inspector_live_updates_detail() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        widget = await _make_workflow(app)
        screen = WorkflowInspectorScreen(widget)
        await app.push_screen(screen)
        await pilot.pause()
        screen._selected_agent = 1
        screen._rendered_log_len = -1
        screen._refresh_detail()

        await widget.handle_workflow_event({
            "kind": "agent_progress",
            "agent_id": 1,
            "message": "grep: found workflow runtime",
        })
        screen._sync()
        await pilot.pause()
        detail_text = " ".join(str(w.render()) for w in screen.query(NoMarkupStatic))
        assert "found workflow runtime" in detail_text


@pytest.mark.asyncio
async def test_inspector_escape_dismisses() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        widget = await _make_workflow(app)
        await app.push_screen(WorkflowInspectorScreen(widget))
        await pilot.pause()
        assert isinstance(app.screen, WorkflowInspectorScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, WorkflowInspectorScreen)


@pytest.mark.asyncio
async def test_inspector_shows_output_for_finished_agent() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        widget = await _make_workflow(app)
        screen = WorkflowInspectorScreen(widget, initial_agent=2)
        await app.push_screen(screen)
        await pilot.pause()
        detail_text = " ".join(str(w.render()) for w in screen.query(NoMarkupStatic))
        assert "textual_ui" in detail_text
        assert "output" in detail_text


@pytest.mark.asyncio
async def test_follow_mode_tracks_latest_running_agent() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        widget = await _make_workflow(app)
        screen = WorkflowInspectorScreen(widget)
        await app.push_screen(screen)
        await pilot.pause()
        assert screen._follow is True
        assert screen._selected_agent == 1

        await widget.handle_workflow_event({
            "kind": "agent_started",
            "agent_id": 3,
            "label": "scan:acp",
            "phase": "Scan",
            "cached": False,
            "prompt": "Analyse vibe/acp.",
        })
        screen._sync()
        await pilot.pause()
        assert screen._selected_agent == 3


@pytest.mark.asyncio
async def test_phase_nodes_show_counts() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        widget = await _make_workflow(app)
        screen = WorkflowInspectorScreen(widget)
        await app.push_screen(screen)
        await pilot.pause()
        phase_node = screen._phase_nodes["Scan"]
        assert "1/2" in str(phase_node.label)


@pytest.mark.asyncio
async def test_agent_row_records_prompt_and_log() -> None:
    app = _Harness()
    async with app.run_test():
        widget = await _make_workflow(app)
        rows: dict[int, WorkflowAgentRow] = widget.agent_rows
        assert rows[1].prompt is not None
        assert rows[1].prompt.startswith("Analyse vibe/core")
        assert rows[1].activity_log == ["▸ Reading vibe/core/agent_loop"]
        assert rows[1].phase_title == "Scan"
