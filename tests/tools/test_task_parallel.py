from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
import time
from typing import Any
from unittest.mock import patch

import pytest

from tests.conftest import build_test_agent_loop, build_test_vibe_config
from tests.mock.task_helpers import make_fake_subagent_loop_factory
from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent_loop import AgentLoop
from vibe.core.agents.models import BuiltinAgentName
from vibe.core.types import (
    AssistantEvent,
    BaseEvent,
    FunctionCall,
    SubagentFinishedEvent,
    ToolCall,
)

AGENT_LOOP_PATH = "vibe.core.tools.builtins.task.AgentLoop"

CHILD_LATENCY = 0.3


def _slow_act_factory(_kwargs: dict[str, Any]) -> Any:
    async def act(task: str) -> AsyncGenerator[BaseEvent, None]:
        yield AssistantEvent(content="starting")
        await asyncio.sleep(CHILD_LATENCY)
        yield AssistantEvent(content=" finished")

    return act


def _task_call(call_id: str, index: int) -> ToolCall:
    return ToolCall(
        id=call_id,
        index=index,
        function=FunctionCall(
            name="task", arguments='{"task": "explore stuff", "agent": "explore"}'
        ),
    )


def make_parent_loop(*, max_parallel: int = 4) -> AgentLoop:
    config = build_test_vibe_config(
        enabled_tools=["task"],
        tools={
            "task": {
                "permission": "always",
                "allowlist": ["explore"],
                "max_parallel": max_parallel,
            }
        },
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
async def test_two_workers_run_concurrently_not_sequentially() -> None:
    factory, created = make_fake_subagent_loop_factory(_slow_act_factory)
    parent = make_parent_loop()

    start = time.monotonic()
    with patch(AGENT_LOOP_PATH, side_effect=factory):
        events = [ev async for ev in parent.act("fan out")]
    elapsed = time.monotonic() - start

    assert len(created) == 2
    assert elapsed < CHILD_LATENCY * 1.75  # ~max(t1, t2), not t1 + t2
    finished = [e for e in events if isinstance(e, SubagentFinishedEvent)]
    assert len(finished) == 2


@pytest.mark.asyncio
async def test_max_parallel_one_serializes_workers() -> None:
    factory, created = make_fake_subagent_loop_factory(_slow_act_factory)
    parent = make_parent_loop(max_parallel=1)

    start = time.monotonic()
    with patch(AGENT_LOOP_PATH, side_effect=factory):
        [ev async for ev in parent.act("fan out")]
    elapsed = time.monotonic() - start

    assert len(created) == 2
    assert elapsed >= CHILD_LATENCY * 2


@pytest.mark.asyncio
async def test_parallel_child_streams_interleave_with_distinct_identities() -> None:
    factory, _ = make_fake_subagent_loop_factory(_slow_act_factory)
    parent = make_parent_loop()

    with patch(AGENT_LOOP_PATH, side_effect=factory):
        events = [ev async for ev in parent.act("fan out")]

    child_sequence = [
        ev.agent.agent_id
        for ev in events
        if isinstance(ev, AssistantEvent) and ev.agent is not None
    ]
    ids = set(child_sequence)
    assert ids == {"child-0", "child-1"}
    # Both children emit before either finishes: their streams interleave.
    first_events = [child_sequence.index(i) for i in ids]
    last_events = [len(child_sequence) - 1 - child_sequence[::-1].index(i) for i in ids]
    assert max(first_events) < min(last_events)
    parents = {
        ev.agent.parent_id
        for ev in events
        if isinstance(ev, AssistantEvent) and ev.agent is not None
    }
    assert parents == {parent.session_id}
