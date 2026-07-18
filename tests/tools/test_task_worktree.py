from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from git import Repo
import pytest

from tests.mock.task_helpers import make_fake_subagent_loop_factory, make_task_ctx
from tests.mock.utils import collect_result
from vibe.core.tools.base import ToolError
from vibe.core.tools.builtins.task import (
    Task,
    TaskArgs,
    TaskResult,
    TaskToolConfig,
    TaskToolState,
)
from vibe.core.types import AssistantEvent, BaseEvent

AGENT_LOOP_PATH = "vibe.core.tools.builtins.task.AgentLoop"


def _init_repo(workdir: Path) -> Repo:
    repo = Repo.init(workdir, initial_branch="main")
    repo.config_writer().set_value("user", "name", "Tester").release()
    repo.config_writer().set_value("user", "email", "t@example.com").release()
    (workdir / "file.txt").write_text("hello\n")
    repo.index.add(["file.txt"])
    repo.index.commit("initial")
    return repo


@pytest.fixture
def git_repo(tmp_working_directory: Path) -> Repo:
    return _init_repo(tmp_working_directory)


def make_task_tool(**config_kwargs: Any) -> Task:
    config = TaskToolConfig(**config_kwargs)
    return Task(config_getter=lambda: config, state=TaskToolState())


def _writing_act_factory(kwargs: dict[str, Any]) -> Any:
    async def act(task: str) -> AsyncGenerator[BaseEvent, None]:
        working_dir: Path = kwargs["working_dir"]
        (working_dir / "worker_output.txt").write_text("done\n")
        yield AssistantEvent(content="wrote worker_output.txt")

    return act


def _patched_writing_loop() -> tuple[Any, list[MagicMock]]:
    return make_fake_subagent_loop_factory(_writing_act_factory)


@pytest.mark.asyncio
async def test_worker_runs_in_isolated_worktree_and_commits(
    git_repo: Repo, tmp_working_directory: Path
) -> None:
    tool = make_task_tool()
    factory, created = _patched_writing_loop()

    with patch(AGENT_LOOP_PATH, side_effect=factory):
        result = await collect_result(
            tool.run(TaskArgs(task="write a file", agent="worker"), make_task_ctx())
        )

    assert isinstance(result, TaskResult)
    assert result.completed is True
    assert result.branch is not None
    assert result.branch.startswith("vibe-worker-")
    assert result.commit is not None
    assert result.files_changed == ["worker_output.txt"]
    assert result.merge_status == "not_attempted"

    assert len(created) == 1
    # The parent checkout is untouched.
    assert not (tmp_working_directory / "worker_output.txt").exists()
    assert not git_repo.git.status("--porcelain").strip()
    # The branch holds the worker's commit.
    branch_commit = git_repo.commit(result.branch)
    assert "worker_output.txt" in branch_commit.stats.files


@pytest.mark.asyncio
async def test_worker_child_loop_gets_worktree_working_dir_and_sandbox(
    git_repo: Repo,
) -> None:
    tool = make_task_tool()

    with patch(AGENT_LOOP_PATH) as loop_cls:
        captured: dict[str, Any] = {}

        def factory(*_args: Any, **kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            return _patched_writing_loop()[0](**kwargs)

        loop_cls.side_effect = factory
        await collect_result(
            tool.run(TaskArgs(task="write a file", agent="worker"), make_task_ctx())
        )

    working_dir = captured["working_dir"]
    assert working_dir is not None
    config = captured["config_orchestrator"].config
    sandbox_glob = config.tools["write_file"]["allowlist"][0]
    assert Path(sandbox_glob).parts[:-1] == Path(working_dir).parts
    assert config.tools["write_file"]["permission"] == "never"
    assert config.tools["edit"]["permission"] == "never"


@pytest.mark.asyncio
async def test_worker_worktree_removed_on_success_but_branch_kept(
    git_repo: Repo,
) -> None:
    tool = make_task_tool(keep_worktrees="on-failure")
    factory, _ = _patched_writing_loop()

    with patch(AGENT_LOOP_PATH, side_effect=factory):
        result = await collect_result(
            tool.run(TaskArgs(task="write a file", agent="worker"), make_task_ctx())
        )

    assert isinstance(result, TaskResult)
    assert result.worktree_path is not None
    assert not Path(result.worktree_path).exists()
    assert result.branch in [h.name for h in git_repo.heads]


@pytest.mark.asyncio
async def test_worker_worktree_kept_always(git_repo: Repo) -> None:
    tool = make_task_tool(keep_worktrees="always")
    factory, _ = _patched_writing_loop()

    with patch(AGENT_LOOP_PATH, side_effect=factory):
        result = await collect_result(
            tool.run(TaskArgs(task="write a file", agent="worker"), make_task_ctx())
        )

    assert isinstance(result, TaskResult)
    assert result.worktree_path is not None
    assert Path(result.worktree_path).exists()


@pytest.mark.asyncio
async def test_worker_worktree_kept_on_failure(git_repo: Repo) -> None:
    tool = make_task_tool(keep_worktrees="on-failure")

    def failing_act_factory(kwargs: dict[str, Any]) -> Any:
        async def act(task: str) -> AsyncGenerator[BaseEvent, None]:
            working_dir: Path = kwargs["working_dir"]
            (working_dir / "partial.txt").write_text("partial\n")
            yield AssistantEvent(content="started")
            raise RuntimeError("boom")

        return act

    factory, _ = make_fake_subagent_loop_factory(failing_act_factory)
    with patch(AGENT_LOOP_PATH, side_effect=factory):
        result = await collect_result(
            tool.run(TaskArgs(task="write a file", agent="worker"), make_task_ctx())
        )

    assert isinstance(result, TaskResult)
    assert result.completed is False
    assert result.worktree_path is not None
    assert Path(result.worktree_path).exists()


@pytest.mark.asyncio
async def test_worker_requires_git_repository(tmp_working_directory: Path) -> None:
    tool = make_task_tool()

    with pytest.raises(ToolError, match="worktree isolation"):
        await collect_result(
            tool.run(TaskArgs(task="write a file", agent="worker"), make_task_ctx())
        )


@pytest.mark.asyncio
async def test_unisolated_write_capable_subagent_is_rejected(git_repo: Repo) -> None:
    from vibe.core.agents.models import AgentProfile, AgentSafety, AgentType

    unsafe = AgentProfile(
        name="unsafe-worker",
        display_name="Unsafe Worker",
        description="writer without isolation",
        safety=AgentSafety.DESTRUCTIVE,
        agent_type=AgentType.SUBAGENT,
        overrides={"enabled_tools": ["edit", "write_file"]},
    )
    ctx = make_task_ctx()
    assert ctx.agent_manager is not None
    with patch.object(ctx.agent_manager, "get_agent", return_value=unsafe):
        tool = make_task_tool()

        with pytest.raises(ToolError, match="not isolated"):
            await collect_result(
                tool.run(TaskArgs(task="write", agent="unsafe-worker"), ctx)
            )


@pytest.mark.asyncio
async def test_auto_merge_merges_clean_branch_into_parent_checkout(
    git_repo: Repo, tmp_working_directory: Path
) -> None:
    tool = make_task_tool(merge="auto")
    factory, _ = _patched_writing_loop()

    with patch(AGENT_LOOP_PATH, side_effect=factory):
        result = await collect_result(
            tool.run(TaskArgs(task="write a file", agent="worker"), make_task_ctx())
        )

    assert isinstance(result, TaskResult)
    assert result.merge_status == "merged"
    assert (tmp_working_directory / "worker_output.txt").read_text() == "done\n"


@pytest.mark.asyncio
async def test_auto_merge_reports_conflicts_and_leaves_checkout_pristine(
    git_repo: Repo, tmp_working_directory: Path
) -> None:
    tool = make_task_tool(merge="auto", keep_worktrees="always")

    def conflicting_act_factory(kwargs: dict[str, Any]) -> Any:
        async def act(task: str) -> AsyncGenerator[BaseEvent, None]:
            working_dir: Path = kwargs["working_dir"]
            (working_dir / "file.txt").write_text("worker version\n")
            yield AssistantEvent(content="edited file.txt")

        return act

    factory, _ = make_fake_subagent_loop_factory(conflicting_act_factory)
    with patch(AGENT_LOOP_PATH, side_effect=factory):
        run = tool.run(TaskArgs(task="edit file", agent="worker"), make_task_ctx())
        # Advance until the worktree exists, then make main diverge before the
        # child finishes so the merge-back sees a conflict.
        items: list[Any] = []
        async for item in run:
            if not items:
                (tmp_working_directory / "file.txt").write_text("main version\n")
                git_repo.index.add(["file.txt"])
                git_repo.index.commit("main diverges")
            items.append(item)

    result = items[-1]
    assert isinstance(result, TaskResult)
    assert result.merge_status == "conflicts"
    assert result.conflicting_paths == ["file.txt"]
    assert (tmp_working_directory / "file.txt").read_text() == "main version\n"
    assert not (tmp_working_directory / ".git" / "MERGE_HEAD").exists()
    assert result.branch in [h.name for h in git_repo.heads]


@pytest.mark.asyncio
async def test_worker_task_text_explains_the_worktree_sandbox(git_repo: Repo) -> None:
    tool = make_task_tool()
    captured: dict[str, str] = {}

    def capturing_act_factory(kwargs: dict[str, Any]) -> Any:
        async def act(task: str) -> AsyncGenerator[BaseEvent, None]:
            captured["task"] = task
            yield AssistantEvent(content="ok")

        return act

    factory, _ = make_fake_subagent_loop_factory(capturing_act_factory)
    with patch(AGENT_LOOP_PATH, side_effect=factory):
        await collect_result(
            tool.run(TaskArgs(task="edit calc.py", agent="worker"), make_task_ctx())
        )

    task_text = captured["task"]
    assert "Working directory:" in task_text
    assert "isolated git worktree" in task_text
    assert "relative paths" in task_text
    assert task_text.endswith("edit calc.py")


@pytest.mark.asyncio
async def test_parent_bypass_tool_permissions_propagates_to_child(
    git_repo: Repo,
) -> None:
    tool = make_task_tool()
    ctx = make_task_ctx()
    ctx.bypass_tool_permissions = True

    with patch(AGENT_LOOP_PATH) as loop_cls:
        captured: dict[str, Any] = {}

        def factory(*_args: Any, **kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            return _patched_writing_loop()[0](**kwargs)

        loop_cls.side_effect = factory
        await collect_result(tool.run(TaskArgs(task="write", agent="worker"), ctx))

    assert captured["force_bypass_tool_permissions"] is True


@pytest.mark.asyncio
async def test_no_bypass_by_default(git_repo: Repo) -> None:
    tool = make_task_tool()

    with patch(AGENT_LOOP_PATH) as loop_cls:
        captured: dict[str, Any] = {}

        def factory(*_args: Any, **kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            return _patched_writing_loop()[0](**kwargs)

        loop_cls.side_effect = factory
        await collect_result(
            tool.run(TaskArgs(task="write", agent="worker"), make_task_ctx())
        )

    assert captured["force_bypass_tool_permissions"] is False


@pytest.mark.asyncio
async def test_no_changes_worker_reports_no_changes(git_repo: Repo) -> None:
    tool = make_task_tool()

    def idle_act_factory(kwargs: dict[str, Any]) -> Any:
        async def act(task: str) -> AsyncGenerator[BaseEvent, None]:
            yield AssistantEvent(content="nothing to do")

        return act

    factory, _ = make_fake_subagent_loop_factory(idle_act_factory)
    with patch(AGENT_LOOP_PATH, side_effect=factory):
        result = await collect_result(
            tool.run(TaskArgs(task="noop", agent="worker"), make_task_ctx())
        )

    assert isinstance(result, TaskResult)
    assert result.merge_status == "no_changes"
    assert result.commit is None
    assert result.files_changed == []
