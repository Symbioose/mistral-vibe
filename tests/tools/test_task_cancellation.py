from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
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
    SubagentStartedEvent,
    ToolCall,
)

AGENT_LOOP_PATH = "vibe.core.tools.builtins.task.AgentLoop"


def _hanging_act_factory(_kwargs: dict[str, Any]) -> Any:
    async def act(task: str) -> AsyncGenerator[BaseEvent, None]:
        yield AssistantEvent(content="starting")
        await asyncio.sleep(60)

    return act


def _task_call(call_id: str, index: int) -> ToolCall:
    return ToolCall(
        id=call_id,
        index=index,
        function=FunctionCall(
            name="task", arguments='{"task": "long work", "agent": "explore"}'
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
async def test_closing_the_parent_turn_closes_both_children() -> None:
    factory, created = make_fake_subagent_loop_factory(_hanging_act_factory)
    parent = make_parent_loop()

    with patch(AGENT_LOOP_PATH, side_effect=factory):
        events = parent.act("fan out")
        started = 0
        async for ev in events:
            if isinstance(ev, SubagentStartedEvent):
                started += 1
            if started == 2:
                break
        await events.aclose()
        # Let cancelled tool tasks unwind.
        await asyncio.sleep(0.05)

    assert len(created) == 2
    for loop in created:
        loop.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_leaves_no_orphan_tool_tasks() -> None:
    factory, _ = make_fake_subagent_loop_factory(_hanging_act_factory)
    parent = make_parent_loop()

    with patch(AGENT_LOOP_PATH, side_effect=factory):
        before = {t for t in asyncio.all_tasks() if not t.done()}
        events = parent.act("fan out")
        async for ev in events:
            if isinstance(ev, SubagentStartedEvent):
                break
        await events.aclose()
        await asyncio.sleep(0.05)
        after = {t for t in asyncio.all_tasks() if not t.done()}

    leaked = after - before
    assert not leaked
