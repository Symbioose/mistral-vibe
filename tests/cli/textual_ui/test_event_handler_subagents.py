from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from tests.stubs.fake_tool import FakeTool, FakeToolArgs
from vibe.cli.textual_ui.handlers.event_handler import EventHandler
from vibe.cli.textual_ui.widgets.cats import KittenMessage, OrchestratorCatMessage
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


def _started_event(call_id: str = "parent-call") -> SubagentStartedEvent:
    return SubagentStartedEvent(
        tool_call_id=call_id,
        task="do work",
        isolation="worktree",
        branch="vibe-worker-abc",
        agent=CHILD,
    )


@pytest.mark.asyncio
async def test_subagent_started_event_mounts_a_kitten_under_the_task_widget() -> None:
    handler, mount_callback = _make_handler()
    task_widget = await handler.handle_event(_task_call_event("parent-call"))
    assert task_widget is not None

    await handler.handle_event(_started_event())

    kittens = [
        call.args[0]
        for call in mount_callback.await_args_list
        if isinstance(call.args[0], KittenMessage)
    ]
    assert len(kittens) == 1
    assert kittens[0].label_text() == "worker · vibe-worker-abc"


@pytest.mark.asyncio
async def test_first_kitten_brings_the_orchestrator_fat_cat() -> None:
    handler, mount_callback = _make_handler()
    await handler.handle_event(_task_call_event("call-a"))
    await handler.handle_event(_task_call_event("call-b"))

    await handler.handle_event(_started_event("call-a"))
    await handler.handle_event(_started_event("call-b"))

    fat_cats = [
        call.args[0]
        for call in mount_callback.await_args_list
        if isinstance(call.args[0], OrchestratorCatMessage)
    ]
    assert len(fat_cats) == 1
    assert fat_cats[0].label_text() == "orchestrator · 2 kittens dispatched"


@pytest.mark.asyncio
async def test_fat_cat_heads_the_group_even_when_a_later_worker_starts_first() -> None:
    handler, mount_callback = _make_handler()
    first_widget = await handler.handle_event(_task_call_event("call-a"))
    await handler.handle_event(_task_call_event("call-b"))
    await handler.handle_event(_task_call_event("call-c"))

    # The LAST dispatched worker is the first to emit its started event.
    await handler.handle_event(_started_event("call-c"))

    fat_cat_mounts = [
        call
        for call in mount_callback.await_args_list
        if isinstance(call.args[0], OrchestratorCatMessage)
    ]
    assert len(fat_cat_mounts) == 1
    assert fat_cat_mounts[0].kwargs["before"] is first_widget


@pytest.mark.asyncio
async def test_subagent_finished_event_updates_the_kitten_status() -> None:
    handler, _ = _make_handler()
    await handler.handle_event(_task_call_event("parent-call"))
    await handler.handle_event(_started_event())
    kitten = handler._kittens["parent-call"]

    await handler.handle_event(
        SubagentFinishedEvent(
            tool_call_id="parent-call",
            status="completed",
            merge_status="no_changes",
            agent=CHILD,
        )
    )

    assert kitten.label_text() == "worker · vibe-worker-abc · completed (no changes)"
    assert "parent-call" not in handler._kittens


@pytest.mark.asyncio
async def test_finished_without_kitten_falls_back_to_stream_message() -> None:
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
async def test_stop_current_tool_call_clears_the_cats() -> None:
    handler, _ = _make_handler()
    await handler.handle_event(_task_call_event("parent-call"))
    await handler.handle_event(_started_event())

    handler.stop_current_tool_call(cancelled=True)

    assert handler._kittens == {}
    assert handler._orchestrator_cat is None


@pytest.mark.asyncio
async def test_unstamped_events_still_render_normally() -> None:
    handler, mount_callback = _make_handler()

    await handler.handle_event(AssistantEvent(content="root speaks"))

    mount_callback.assert_awaited()
