from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest

from tests.mock.task_helpers import make_fake_subagent_loop, make_task_ctx
from vibe.core.tools.builtins.task import Task, TaskArgs, TaskResult, TaskToolState
from vibe.core.tools.builtins.todo import Todo
from vibe.core.types import (
    AgentStats,
    AssistantEvent,
    BaseEvent,
    ReasoningEvent,
    SubagentFinishedEvent,
    SubagentStartedEvent,
    ToolCallEvent,
    ToolResultEvent,
    UserMessageEvent,
)

AGENT_LOOP_PATH = "vibe.core.tools.builtins.task.AgentLoop"


async def scripted_act(task: str) -> AsyncGenerator[BaseEvent, None]:
    yield UserMessageEvent(content=task, message_id="m0")
    yield ReasoningEvent(content="thinking", message_id="m1")
    yield AssistantEvent(content="Let me look.", message_id="m2")
    yield ToolCallEvent(tool_call_id="child-call-1", tool_name="todo", tool_class=Todo)
    yield ToolResultEvent(
        tool_call_id="child-call-1", tool_name="todo", tool_class=Todo
    )
    yield AssistantEvent(content=" Done.", message_id="m3")


@pytest.fixture
def task_tool() -> Task:
    from vibe.core.tools.builtins.task import TaskToolConfig

    return Task(config_getter=lambda: TaskToolConfig(), state=TaskToolState())


async def run_and_collect(task_tool: Task) -> list[BaseEvent | TaskResult]:
    ctx = make_task_ctx(tool_call_id="parent-call", session_id="parent-session")
    loop = make_fake_subagent_loop(
        scripted_act,
        session_id="child-session",
        stats=AgentStats(session_prompt_tokens=7, session_completion_tokens=3),
    )
    with patch(AGENT_LOOP_PATH, return_value=loop):
        return [
            item
            async for item in task_tool.run(
                TaskArgs(task="look around", agent="explore"), ctx
            )
        ]


@pytest.mark.asyncio
async def test_lifecycle_events_bracket_the_child_stream(task_tool: Task) -> None:
    items = await run_and_collect(task_tool)

    assert isinstance(items[0], SubagentStartedEvent)
    assert items[0].tool_call_id == "parent-call"
    assert items[0].task == "look around"
    assert items[0].isolation == "none"
    assert isinstance(items[-2], SubagentFinishedEvent)
    assert isinstance(items[-1], TaskResult)
    finished = items[-2]
    assert finished.status == "completed"
    assert finished.prompt_tokens == 7
    assert finished.completion_tokens == 3


@pytest.mark.asyncio
async def test_forwarded_child_events_carry_agent_identity(task_tool: Task) -> None:
    items = await run_and_collect(task_tool)

    stamped = [e for e in items if isinstance(e, BaseEvent) and e.agent is not None]
    assert all(e.agent is not None for e in stamped)
    identities = {
        (e.agent.agent_id, e.agent.parent_id, e.agent.name) for e in stamped if e.agent
    }
    assert identities == {("child-session", "parent-session", "explore")}

    forwarded_types = [
        type(e)
        for e in stamped
        if not isinstance(e, SubagentStartedEvent | SubagentFinishedEvent)
    ]
    assert forwarded_types == [
        ReasoningEvent,
        AssistantEvent,
        ToolCallEvent,
        ToolResultEvent,
        AssistantEvent,
    ]


@pytest.mark.asyncio
async def test_child_user_message_events_are_not_forwarded(task_tool: Task) -> None:
    items = await run_and_collect(task_tool)

    assert not any(isinstance(e, UserMessageEvent) for e in items)


@pytest.mark.asyncio
async def test_forwarded_events_are_verbatim_copies(task_tool: Task) -> None:
    items = await run_and_collect(task_tool)

    assistants = [
        e for e in items if isinstance(e, AssistantEvent) and e.agent is not None
    ]
    assert [e.content for e in assistants] == ["Let me look.", " Done."]
    assert [e.message_id for e in assistants] == ["m2", "m3"]


@pytest.mark.asyncio
async def test_task_result_rolls_up_child_usage(task_tool: Task) -> None:
    items = await run_and_collect(task_tool)

    result = items[-1]
    assert isinstance(result, TaskResult)
    assert result.agent_id == "child-session"
    assert result.prompt_tokens == 7
    assert result.completion_tokens == 3
    assert result.response == "Let me look. Done."
