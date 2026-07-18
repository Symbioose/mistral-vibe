from __future__ import annotations

import pytest

from vibe.core.tools.builtins.task import Task, TaskResult
from vibe.core.types import ToolResultEvent


def _event(result: TaskResult) -> ToolResultEvent:
    return ToolResultEvent(
        tool_name="task", tool_class=Task, result=result, tool_call_id="call_1"
    )


def _result(**kwargs: object) -> TaskResult:
    defaults: dict[str, object] = {
        "response": "done",
        "turns_used": 3,
        "completed": True,
    }
    return TaskResult.model_validate({**defaults, **kwargs})


@pytest.mark.parametrize(
    ("merge_status", "expected_suffix"),
    [
        ("merged", "[vibe-worker-x: merged]"),
        ("conflicts", "[vibe-worker-x: conflicts]"),
        ("no_changes", "[no changes]"),
        ("not_attempted", "[branch vibe-worker-x]"),
    ],
)
def test_branch_suffix_is_human_readable(
    merge_status: str, expected_suffix: str
) -> None:
    display = Task.get_result_display(
        _event(_result(branch="vibe-worker-x", merge_status=merge_status))
    )

    assert display.suffix == expected_suffix


def test_no_suffix_without_a_branch() -> None:
    display = Task.get_result_display(_event(_result()))

    assert display.suffix == ""
    assert display.message == "Agent completed in 3 turns"
