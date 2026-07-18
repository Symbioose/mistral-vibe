from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from vibe.core.tools.base import InvokeContext, ToolError
import vibe.core.tools.builtins.workflow as workflow_module
from vibe.core.tools.builtins.workflow import (
    Workflow,
    WorkflowArgs,
    WorkflowResult,
    WorkflowToolConfig,
)
from vibe.core.types import ToolStreamEvent
from vibe.core.workflows.models import SubagentOutcome, SubagentRequest, WorkflowStatus

SCRIPT = """
meta = {"name": "demo", "description": "demo workflow", "phases": [{"title": "Go"}]}
phase("Go")
outs = await parallel([lambda: agent("alpha"), lambda: agent("beta")])
log("both done")
result({"outs": outs})
"""


class FakeSpawner:
    calls: list[SubagentRequest] = []

    def __init__(self, _ctx: InvokeContext, _config: WorkflowToolConfig) -> None:
        pass

    async def run(
        self, request: SubagentRequest, on_progress: Callable[[str], None]
    ) -> SubagentOutcome:
        FakeSpawner.calls.append(request)
        on_progress("working")
        return SubagentOutcome(success=True, text=f"echo:{request.prompt}")


@pytest.fixture
def fake_spawner(monkeypatch: pytest.MonkeyPatch) -> type[FakeSpawner]:
    FakeSpawner.calls = []
    monkeypatch.setattr(workflow_module, "_AgentLoopSpawner", FakeSpawner)
    return FakeSpawner


def make_tool() -> Workflow:
    return Workflow.from_config(lambda: WorkflowToolConfig())


async def collect(
    tool: Workflow, args: WorkflowArgs, ctx: InvokeContext
) -> tuple[list[ToolStreamEvent], WorkflowResult]:
    stream: list[ToolStreamEvent] = []
    final: WorkflowResult | None = None
    async for item in tool.run(args, ctx):
        if isinstance(item, ToolStreamEvent):
            stream.append(item)
        else:
            final = item
    assert final is not None
    return stream, final


@pytest.mark.asyncio
async def test_tool_runs_script_end_to_end(
    tmp_path: Path, fake_spawner: type[FakeSpawner]
) -> None:
    tool = make_tool()
    ctx = InvokeContext(tool_call_id="tc1", session_dir=tmp_path)
    stream, final = await collect(tool, WorkflowArgs(script=SCRIPT), ctx)

    assert final.status is WorkflowStatus.COMPLETED
    assert final.name == "demo"
    assert final.result == {"outs": ["echo:alpha", "echo:beta"]}
    assert final.agents_spawned == 2
    assert len(fake_spawner.calls) == 2

    messages = [e.message for e in stream]
    assert "Phase: Go" in messages
    assert "both done" in messages
    kinds = [e.data["kind"] for e in stream if e.data]
    assert "agent_started" in kinds
    assert "agent_finished" in kinds

    run_dir = tmp_path / "workflows" / final.run_id
    assert (run_dir / "script.py").read_text(encoding="utf-8") == SCRIPT
    assert (run_dir / "journal.jsonl").exists()
    assert (run_dir / "result.json").exists()


@pytest.mark.asyncio
async def test_tool_resume_replays_journal(
    tmp_path: Path, fake_spawner: type[FakeSpawner]
) -> None:
    tool = make_tool()
    ctx = InvokeContext(tool_call_id="tc1", session_dir=tmp_path)
    _stream, first = await collect(tool, WorkflowArgs(script=SCRIPT), ctx)
    assert len(fake_spawner.calls) == 2

    _stream, second = await collect(
        tool, WorkflowArgs(script=SCRIPT, resume_from_run_id=first.run_id), ctx
    )
    assert second.status is WorkflowStatus.COMPLETED
    assert second.result == first.result
    assert second.agents_cached == 2
    assert len(fake_spawner.calls) == 2


@pytest.mark.asyncio
async def test_tool_resume_unknown_run_id(
    tmp_path: Path, fake_spawner: type[FakeSpawner]
) -> None:
    tool = make_tool()
    ctx = InvokeContext(tool_call_id="tc1", session_dir=tmp_path)
    with pytest.raises(ToolError, match="No journal"):
        await collect(
            tool, WorkflowArgs(script=SCRIPT, resume_from_run_id="wf_missing"), ctx
        )


@pytest.mark.asyncio
async def test_tool_rejects_invalid_script(tmp_path: Path) -> None:
    tool = make_tool()
    ctx = InvokeContext(tool_call_id="tc1", session_dir=tmp_path)
    with pytest.raises(ToolError, match="Invalid workflow script"):
        await collect(tool, WorkflowArgs(script="x = 1"), ctx)


@pytest.mark.asyncio
async def test_tool_requires_script_or_path(tmp_path: Path) -> None:
    tool = make_tool()
    ctx = InvokeContext(tool_call_id="tc1", session_dir=tmp_path)
    with pytest.raises(ToolError, match="script"):
        await collect(tool, WorkflowArgs(), ctx)


@pytest.mark.asyncio
async def test_tool_loads_script_from_path(
    tmp_path: Path, fake_spawner: type[FakeSpawner]
) -> None:
    script_file = tmp_path / "my_workflow.py"
    script_file.write_text(SCRIPT, encoding="utf-8")
    tool = make_tool()
    ctx = InvokeContext(tool_call_id="tc1", session_dir=tmp_path)
    _stream, final = await collect(
        tool, WorkflowArgs(script_path=str(script_file)), ctx
    )
    assert final.status is WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_failed_workflow_reports_error_and_resume_hint(
    tmp_path: Path, fake_spawner: type[FakeSpawner]
) -> None:
    bad = 'meta = {"name": "bad", "description": "d"}\nawait agent("a")\nboom()'
    tool = make_tool()
    ctx = InvokeContext(tool_call_id="tc1", session_dir=tmp_path)
    _stream, final = await collect(tool, WorkflowArgs(script=bad), ctx)
    assert final.status is WorkflowStatus.FAILED
    assert final.error is not None
    assert "boom" in final.error
    extra = tool.get_result_extra(final)
    assert extra is not None
    assert final.run_id in extra


def test_tool_name_and_description() -> None:
    assert Workflow.get_name() == "workflow"
    description = Workflow.get_full_description()
    assert "deterministic" in description
    parameters = Workflow.get_parameters()
    assert "script" in parameters["properties"]
    assert "resume_from_run_id" in parameters["properties"]


def test_call_display_parses_meta() -> None:
    from vibe.core.types import ToolCallEvent

    event = ToolCallEvent(
        tool_call_id="tc1",
        tool_name="workflow",
        tool_class=Workflow,
        args=WorkflowArgs(script=SCRIPT),
    )
    display = Workflow.get_call_display(event)
    assert "demo" in display.summary


def test_result_truncation() -> None:
    big = "x" * 100_000
    bounded: Any = workflow_module._bounded_result(big)
    assert bounded["truncated"] is True
    assert len(bounded["preview"]) <= 40_000
    assert workflow_module._bounded_result({"a": 1}) == {"a": 1}
