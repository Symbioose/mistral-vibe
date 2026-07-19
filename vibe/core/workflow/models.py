from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WorkflowPhaseMeta(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    title: str = Field(min_length=1, max_length=80)
    detail: str | None = None
    model: str | None = None


class WorkflowMeta(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$", max_length=64)
    description: str = Field(min_length=1, max_length=200)
    when_to_use: str | None = Field(default=None, alias="whenToUse")
    phases: list[WorkflowPhaseMeta] = Field(default_factory=list)


class AgentRunStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"


class WorkflowStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SubagentRequest(BaseModel):
    prompt: str
    agent_name: str | None = None
    model: str | None = None


class SubagentOutcome(BaseModel):
    success: bool
    text: str = ""
    error: str | None = None
    turns_used: int = 0


class WorkflowRunOutcome(BaseModel):
    status: WorkflowStatus
    value: Any = None
    error: str | None = None
    agents_spawned: int = 0
    agents_cached: int = 0
    duration_s: float = 0.0
