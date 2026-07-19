from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import build_test_agent_loop, build_test_vibe_config
from tests.mock.task_helpers import make_fake_subagent_loop
from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent_loop import AgentLoop
from vibe.core.agents.models import BuiltinAgentName
from vibe.core.tools.builtins.task import TaskResult
from vibe.core.types import (
    AssistantEvent,
    BaseEvent,
    FunctionCall,
    SubagentFinishedEvent,
    ToolCall,
    ToolResultEvent,
)

AGENT_LOOP_PATH = "vibe.core.tools.builtins.task.AgentLoop"


async def failing_act(task: str) -> AsyncGenerator[BaseEvent, None]:
    yield AssistantEvent(content="about to fail")
    raise RuntimeError("worker A exploded")


async def healthy_act(task: str) -> AsyncGenerator[BaseEvent, None]:
    yield AssistantEvent(content="worker B fine")


def _task_call(call_id: str, index: int) -> ToolCall:
    return ToolCall(
        id=call_id,
        index=index,
        function=FunctionCall(
            name="task", arguments='{"task": "do work", "agent": "explore"}'
        ),
    )


def make_parent_loop() -> AgentLoop:
    config = build_test_vibe_config(
        enabled_tools=["task"],
        tools={"task": {"permission": "always", "allowlist": ["explore"]}},
    )
    return build_test_agent_loop(
        config=config,
        agent_name=BuiltinAgentName.AUTO_APPROVE,
        backend=FakeBackend([
            [
                mock_llm_chunk(
                    content="Fanning out.",
                    tool_calls=[_task_call("call_a", 0), _task_call("call_b", 1)],
                )
            ],
            [mock_llm_chunk(content="Done.")],
        ]),
    )


@pytest.mark.asyncio
async def test_one_failing_worker_does_not_cancel_its_sibling() -> None:
    created: list[MagicMock] = []

    def factory(*_args: Any, **_kwargs: Any) -> MagicMock:
        act = failing_act if not created else healthy_act
        loop = make_fake_subagent_loop(act, session_id=f"child-{len(created)}")
        created.append(loop)
        return loop

    parent = make_parent_loop()
    with patch(AGENT_LOOP_PATH, side_effect=factory):
        events = [ev async for ev in parent.act("fan out")]

    results = [
        ev.result
        for ev in events
        if isinstance(ev, ToolResultEvent)
        and ev.agent is None
        and isinstance(ev.result, TaskResult)
    ]
    assert len(results) == 2
    by_completed = {r.completed: r for r in results}
    assert by_completed[False].response.startswith("about to fail")
    assert "worker A exploded" in by_completed[False].response
    assert by_completed[True].response == "worker B fine"

    statuses = {ev.status for ev in events if isinstance(ev, SubagentFinishedEvent)}
    assert statuses == {"completed", "failed"}
    # Both children were closed and the parent turn ran to completion.
    for loop in created:
        loop.aclose.assert_awaited_once()
    final_assistants = [
        ev for ev in events if isinstance(ev, AssistantEvent) and ev.agent is None
    ]
    assert final_assistants[-1].content == "Done."
