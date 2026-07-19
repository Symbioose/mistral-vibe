from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from vibe.core.workflow.models import AgentRunStatus, WorkflowStatus


class PhaseStartedEvent(BaseModel):
    kind: Literal["phase_started"] = "phase_started"
    title: str


class AgentStartedEvent(BaseModel):
    kind: Literal["agent_started"] = "agent_started"
    agent_id: int
    label: str
    phase: str | None = None
    cached: bool = False
    prompt: str | None = None


class AgentProgressEvent(BaseModel):
    kind: Literal["agent_progress"] = "agent_progress"
    agent_id: int
    message: str


class AgentFinishedEvent(BaseModel):
    kind: Literal["agent_finished"] = "agent_finished"
    agent_id: int
    label: str | None = None
    status: AgentRunStatus
    cached: bool = False
    duration_s: float | None = None
    detail: str | None = None
    output: str | None = None


class WorkflowLogEvent(BaseModel):
    kind: Literal["log"] = "log"
    message: str


class WorkflowFinishedEvent(BaseModel):
    kind: Literal["workflow_finished"] = "workflow_finished"
    status: WorkflowStatus
    agents_spawned: int
    agents_cached: int
    duration_s: float
    error: str | None = None


WorkflowEvent = Annotated[
    PhaseStartedEvent
    | AgentStartedEvent
    | AgentProgressEvent
    | AgentFinishedEvent
    | WorkflowLogEvent
    | WorkflowFinishedEvent,
    Field(discriminator="kind"),
]
