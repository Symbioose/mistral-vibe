from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError
import pytest

from vibe.core.types import (
    AgentIdentity,
    AssistantEvent,
    SubagentFinishedEvent,
    SubagentStartedEvent,
    ToolResultEvent,
)


def test_agent_identity_is_frozen() -> None:
    identity = AgentIdentity(agent_id="child-1", parent_id="root-1", name="worker")
    with pytest.raises(ValidationError):
        identity.agent_id = "other"


def test_events_default_to_no_agent_identity() -> None:
    event = AssistantEvent(content="hello")
    assert event.agent is None


def test_events_round_trip_agent_identity() -> None:
    identity = AgentIdentity(agent_id="child-1", parent_id="root-1", name="worker")
    event = AssistantEvent(content="hello", agent=identity)
    restored = AssistantEvent.model_validate(event.model_dump())
    assert restored.agent == identity


def test_assistant_event_add_preserves_agent_identity() -> None:
    identity = AgentIdentity(agent_id="child-1", parent_id=None, name="worker")
    merged = AssistantEvent(content="a", agent=identity) + AssistantEvent(content="b")
    assert merged.content == "ab"
    assert merged.agent == identity


def test_tool_result_event_accepts_agent_identity() -> None:
    identity = AgentIdentity(agent_id="child-1", parent_id="root-1", name="worker")
    event = ToolResultEvent(
        tool_name="edit", tool_class=None, tool_call_id="call_1", agent=identity
    )
    assert event.agent is not None
    assert event.agent.parent_id == "root-1"


def test_subagent_started_event_round_trip() -> None:
    identity = AgentIdentity(agent_id="child-1", parent_id="root-1", name="worker")
    event = SubagentStartedEvent(
        tool_call_id="call_1",
        task="implement feature",
        isolation="worktree",
        branch="vibe-worker-abc123",
        worktree_path=Path("/tmp/worktrees/vibe-worker-abc123"),
        agent=identity,
    )
    restored = SubagentStartedEvent.model_validate(event.model_dump())
    assert restored == event


def test_subagent_finished_event_defaults_and_round_trip() -> None:
    event = SubagentFinishedEvent(tool_call_id="call_1", status="completed")
    assert event.turns_used == 0
    assert event.merge_status == "not_attempted"
    restored = SubagentFinishedEvent.model_validate(event.model_dump())
    assert restored == event


def test_subagent_finished_event_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        SubagentFinishedEvent.model_validate({
            "tool_call_id": "call_1",
            "status": "exploded",
        })
