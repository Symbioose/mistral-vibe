from __future__ import annotations

from collections.abc import AsyncGenerator

from pydantic import BaseModel
import pytest

from tests.conftest import build_test_agent_loop, build_test_vibe_config
from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.core.agent_loop import AgentLoop
from vibe.core.agents.models import BuiltinAgentName
from vibe.core.tools.base import BaseTool, BaseToolConfig, BaseToolState, InvokeContext
from vibe.core.types import (
    AgentIdentity,
    AssistantEvent,
    BaseEvent,
    FunctionCall,
    ToolCall,
    ToolResultEvent,
    ToolStreamEvent,
)

CHILD_IDENTITY = AgentIdentity(agent_id="child-1", parent_id="root-1", name="worker")


class ForwardingToolArgs(BaseModel):
    text: str = ""


class ForwardingToolResult(BaseModel):
    message: str = "done"


class ForwardingTool(
    BaseTool[ForwardingToolArgs, ForwardingToolResult, BaseToolConfig, BaseToolState]
):
    @classmethod
    def get_name(cls) -> str:
        return "forwarding_tool"

    async def run(
        self, args: ForwardingToolArgs, ctx: InvokeContext | None = None
    ) -> AsyncGenerator[BaseEvent | ForwardingToolResult, None]:
        tool_call_id = ctx.tool_call_id if ctx else ""
        yield AssistantEvent(content="child says hi", agent=CHILD_IDENTITY)
        yield ToolStreamEvent(
            tool_name=self.get_name(), message="streaming", tool_call_id=tool_call_id
        )
        yield ForwardingToolResult(message=args.text or "done")


def make_forwarding_loop() -> AgentLoop:
    tool_call = ToolCall(
        id="call_fwd",
        index=0,
        function=FunctionCall(name="forwarding_tool", arguments="{}"),
    )
    config = build_test_vibe_config(enabled_tools=["forwarding_tool"])
    agent_loop = build_test_agent_loop(
        config=config,
        agent_name=BuiltinAgentName.AUTO_APPROVE,
        backend=FakeBackend([
            [mock_llm_chunk(content="Calling.", tool_calls=[tool_call])],
            [mock_llm_chunk(content="Done.")],
        ]),
    )
    agent_loop.tool_manager._all_tools["forwarding_tool"] = ForwardingTool
    return agent_loop


@pytest.mark.asyncio
async def test_stamped_events_yielded_by_tools_are_forwarded_verbatim() -> None:
    agent_loop = make_forwarding_loop()

    events = [ev async for ev in agent_loop.act("go")]

    forwarded = [
        e for e in events if isinstance(e, AssistantEvent) and e.agent is not None
    ]
    assert len(forwarded) == 1
    assert forwarded[0].content == "child says hi"
    assert forwarded[0].agent == CHILD_IDENTITY


@pytest.mark.asyncio
async def test_tool_stream_events_still_forwarded_and_result_still_consumed() -> None:
    agent_loop = make_forwarding_loop()

    events = [ev async for ev in agent_loop.act("go")]

    streams = [e for e in events if isinstance(e, ToolStreamEvent)]
    assert any(s.message == "streaming" for s in streams)
    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(results) == 1
    assert results[0].error is None
    assert isinstance(results[0].result, ForwardingToolResult)


@pytest.mark.asyncio
async def test_root_events_carry_no_agent_identity() -> None:
    agent_loop = make_forwarding_loop()

    events = [ev async for ev in agent_loop.act("go")]

    root_events = [
        e
        for e in events
        if not (isinstance(e, AssistantEvent) and e.content == "child says hi")
    ]
    assert root_events
    assert all(e.agent is None for e in root_events)
