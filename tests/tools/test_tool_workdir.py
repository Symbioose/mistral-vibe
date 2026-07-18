from __future__ import annotations

from pathlib import Path

import pytest

from tests.mock.utils import collect_result
from vibe.core.tools.base import BaseToolState, ToolPermission
from vibe.core.tools.builtins.bash import Bash, BashArgs, BashToolConfig
from vibe.core.tools.builtins.edit import Edit, EditArgs, EditConfig
from vibe.core.tools.builtins.read_file import (
    ReadFile,
    ReadFileArgs,
    ReadFileConfig,
    ReadFileState,
)
from vibe.core.tools.builtins.write_file import (
    WriteFile,
    WriteFileArgs,
    WriteFileConfig,
)
from vibe.core.tools.utils import resolve_file_tool_permission
from vibe.core.utils import is_windows


@pytest.fixture
def workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("workdir")


def make_write_file(workdir: Path) -> WriteFile:
    return WriteFile(
        config_getter=lambda: WriteFileConfig(),
        state=BaseToolState(),
        workdir_getter=lambda: workdir,
    )


def test_workdir_defaults_to_cwd() -> None:
    tool = WriteFile(config_getter=lambda: WriteFileConfig(), state=BaseToolState())
    assert tool.workdir == Path.cwd()


def test_workdir_getter_is_honored(workdir: Path) -> None:
    assert make_write_file(workdir).workdir == workdir


@pytest.mark.asyncio
async def test_write_file_anchors_relative_paths_in_workdir(workdir: Path) -> None:
    tool = make_write_file(workdir)

    result = await collect_result(
        tool.run(WriteFileArgs(file_path="sub/new.txt", content="hello"))
    )

    written = Path(result.file_path)
    assert written.is_relative_to(workdir.resolve())
    assert written.read_text(encoding="utf-8") == "hello"
    assert not (Path.cwd() / "sub" / "new.txt").exists()


@pytest.mark.asyncio
async def test_edit_anchors_relative_paths_in_workdir(workdir: Path) -> None:
    (workdir / "target.txt").write_text("old content", encoding="utf-8")
    tool = Edit(
        config_getter=lambda: EditConfig(),
        state=BaseToolState(),
        workdir_getter=lambda: workdir,
    )

    await collect_result(
        tool.run(EditArgs(file_path="target.txt", old_string="old", new_string="new"))
    )

    assert (workdir / "target.txt").read_text(encoding="utf-8") == "new content"


@pytest.mark.asyncio
async def test_read_file_anchors_relative_paths_in_workdir(workdir: Path) -> None:
    (workdir / "data.txt").write_text("workdir content", encoding="utf-8")
    tool = ReadFile(
        config_getter=lambda: ReadFileConfig(),
        state=ReadFileState(),
        workdir_getter=lambda: workdir,
    )

    result = await collect_result(tool.run(ReadFileArgs(file_path="data.txt")))

    assert "workdir content" in result.content


@pytest.mark.asyncio
@pytest.mark.skipif(is_windows(), reason="asserts against a POSIX pwd")
async def test_bash_runs_subprocess_in_workdir(workdir: Path) -> None:
    tool = Bash(
        config_getter=lambda: BashToolConfig(),
        state=BaseToolState(),
        workdir_getter=lambda: workdir,
    )

    result = await collect_result(tool.run(BashArgs(command="pwd")))

    assert Path(result.stdout.strip()).resolve() == workdir.resolve()


def test_resolve_permission_matches_allowlist_against_workdir(workdir: Path) -> None:
    ctx = resolve_file_tool_permission(
        "inside.txt",
        tool_name="write_file",
        allowlist=[str(workdir / "**")],
        denylist=[],
        config_permission=ToolPermission.NEVER,
        sensitive_patterns=[],
        workdir=workdir,
    )

    assert ctx is not None
    assert ctx.permission is ToolPermission.ALWAYS


def test_resolve_permission_denies_outside_workdir_when_never(
    workdir: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    ctx = resolve_file_tool_permission(
        str(outside),
        tool_name="write_file",
        allowlist=[str(workdir / "**")],
        denylist=[],
        config_permission=ToolPermission.NEVER,
        sensitive_patterns=[],
        workdir=workdir,
    )

    assert ctx is not None
    assert ctx.permission is ToolPermission.NEVER
