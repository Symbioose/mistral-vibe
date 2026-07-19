from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import build_test_vibe_config
from tests.mock.utils import collect_result
from vibe.core.tools.base import BaseToolState, ToolPermission
from vibe.core.tools.builtins.task import _apply_sandbox_overrides
from vibe.core.tools.builtins.write_file import (
    WriteFile,
    WriteFileArgs,
    WriteFileConfig,
)
from vibe.core.worktree import PreparedWorktree


def make_worktree(root: Path) -> PreparedWorktree:
    return PreparedWorktree(
        name="vibe-worker-abc",
        branch="vibe-worker-abc",
        root=root,
        path=root,
        repo_root=root.parent,
        base_commit="0" * 40,
        created=True,
        branch_created=True,
    )


def test_apply_sandbox_overrides_pins_write_tools_to_worktree(tmp_path: Path) -> None:
    config = build_test_vibe_config(tools={"edit": {"allowlist": ["/existing/**"]}})
    worktree = make_worktree(tmp_path / "wt")

    _apply_sandbox_overrides(config, worktree)

    sandbox_glob = str((tmp_path / "wt") / "**")
    assert config.tools["write_file"] == {
        "permission": "never",
        "allowlist": [sandbox_glob],
    }
    assert config.tools["edit"]["permission"] == "never"
    assert config.tools["edit"]["allowlist"] == [sandbox_glob, "/existing/**"]


class TestSandboxedWriteFile:
    @pytest.fixture
    def worktree_root(self, tmp_path_factory: pytest.TempPathFactory) -> Path:
        return tmp_path_factory.mktemp("worktree")

    @pytest.fixture
    def sandboxed_write_file(self, worktree_root: Path) -> WriteFile:
        config = WriteFileConfig(
            permission=ToolPermission.NEVER, allowlist=[str(worktree_root / "**")]
        )
        return WriteFile(
            config_getter=lambda: config,
            state=BaseToolState(),
            workdir_getter=lambda: worktree_root,
        )

    def test_relative_path_inside_worktree_is_auto_approved(
        self, sandboxed_write_file: WriteFile
    ) -> None:
        ctx = sandboxed_write_file.resolve_permission(
            WriteFileArgs(file_path="src/new.py", content="x")
        )

        assert ctx is not None
        assert ctx.permission is ToolPermission.ALWAYS

    def test_absolute_path_inside_worktree_is_auto_approved(
        self, sandboxed_write_file: WriteFile, worktree_root: Path
    ) -> None:
        ctx = sandboxed_write_file.resolve_permission(
            WriteFileArgs(
                file_path=str(worktree_root / "deep" / "file.py"), content="x"
            )
        )

        assert ctx is not None
        assert ctx.permission is ToolPermission.ALWAYS

    def test_path_outside_worktree_is_denied_not_asked(
        self, sandboxed_write_file: WriteFile, tmp_path: Path
    ) -> None:
        ctx = sandboxed_write_file.resolve_permission(
            WriteFileArgs(file_path=str(tmp_path / "escape.py"), content="x")
        )

        assert ctx is not None
        assert ctx.permission is ToolPermission.NEVER

    def test_parent_checkout_path_is_denied(
        self, sandboxed_write_file: WriteFile, tmp_working_directory: Path
    ) -> None:
        # tmp_working_directory is the (trusted) parent checkout: writes there
        # fall through the allowlist to the NEVER config permission.
        ctx = sandboxed_write_file.resolve_permission(
            WriteFileArgs(file_path=str(tmp_working_directory / "main.py"), content="x")
        )

        assert ctx is None or ctx.permission is ToolPermission.NEVER

    @pytest.mark.asyncio
    async def test_write_inside_worktree_executes_without_any_prompt(
        self, sandboxed_write_file: WriteFile, worktree_root: Path
    ) -> None:
        result = await collect_result(
            sandboxed_write_file.run(
                WriteFileArgs(file_path="out.txt", content="sandboxed")
            )
        )

        written = Path(result.file_path)
        assert written.is_relative_to(worktree_root.resolve())
        assert written.read_text(encoding="utf-8") == "sandboxed"
