from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from tests.stubs.fake_tool import FakeTool, FakeToolArgs
from vibe.cli.textual_ui.handlers.event_handler import EventHandler
from vibe.core.types import (
    AgentIdentity,
    AssistantEvent,
    SubagentFinishedEvent,
    SubagentStartedEvent,
    ToolCallEvent,
    ToolResultEvent,
)

CHILD = AgentIdentity(agent_id="child-1", parent_id="root-1", name="worker")


def _make_handler() -> tuple[EventHandler, AsyncMock]:
    mount_callback = AsyncMock()
    handler = EventHandler(
        mount_callback=mount_callback, get_tools_collapsed=lambda: False
    )
    return handler, mount_callback


def _task_call_event(call_id: str) -> ToolCallEvent:
    return ToolCallEvent(
        tool_name="task", tool_class=FakeTool, args=FakeToolArgs(), tool_call_id=call_id
    )


@pytest.mark.asyncio
async def test_stamped_child_events_do_not_reach_the_transcript() -> None:
    handler, mount_callback = _make_handler()

    await handler.handle_event(AssistantEvent(content="child says hi", agent=CHILD))
    await handler.handle_event(
        ToolResultEvent(
            tool_name="edit",
            tool_class=FakeTool,
            tool_call_id="child-call",
            agent=CHILD,
        )
    )

    mount_callback.assert_not_awaited()
    assert handler.current_streaming_message is None


@pytest.mark.asyncio
async def test_subagent_started_event_streams_on_owning_task_widget() -> None:
    handler, _ = _make_handler()
    task_widget = await handler.handle_event(_task_call_event("parent-call"))
    assert task_widget is not None
    task_widget.set_stream_message = Mock()

    await handler.handle_event(
        SubagentStartedEvent(
            tool_call_id="parent-call",
            task="do work",
            isolation="worktree",
            branch="vibe-worker-abc",
            agent=CHILD,
        )
    )

    task_widget.set_stream_message.assert_called_once_with(
        "worker: started on branch vibe-worker-abc"
    )


@pytest.mark.asyncio
async def test_subagent_finished_event_streams_status_and_merge() -> None:
    handler, _ = _make_handler()
    task_widget = await handler.handle_event(_task_call_event("parent-call"))
    assert task_widget is not None
    task_widget.set_stream_message = Mock()

    await handler.handle_event(
        SubagentFinishedEvent(
            tool_call_id="parent-call",
            status="completed",
            merge_status="no_changes",
            agent=CHILD,
        )
    )

    task_widget.set_stream_message.assert_called_once_with(
        "worker: completed (no changes)"
    )


@pytest.mark.asyncio
async def test_unstamped_events_still_render_normally() -> None:
    handler, mount_callback = _make_handler()

    await handler.handle_event(AssistantEvent(content="root speaks"))

    mount_callback.assert_awaited()
