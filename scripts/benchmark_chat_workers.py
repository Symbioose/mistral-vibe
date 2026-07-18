"""Benchmark the parallel-workers value: solo agent vs 3 isolated workers.

Both conditions implement the same 9 stubbed functions (demo-chat corpus,
executable ground truth = unittest). Solo: one write-capable agent does all
three modules sequentially. Workers: three agents run CONCURRENTLY, each in
its own git worktree on its own module; the bench commits each worktree
branch and merges all three back into main. Success is measured by running
the test suite, not by claims.

Usage: uv run python scripts/benchmark_chat_workers.py --out DIR
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import aclosing, suppress
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

from vibe.core.config.harness_files import init_harness_files_manager

init_harness_files_manager()

from vibe.core.agent_loop import AgentLoop
from vibe.core.config import SessionLoggingConfig, VibeConfig
from vibe.core.config.orchestrator_legacy import LegacyConfigOrchestrator
from vibe.core.types import AssistantEvent


def project_modules(project: Path) -> list[str]:
    return sorted(p.stem for p in project.glob("*.py"))

WORKER_BRIEF = (
    "Implement EVERY function currently raising NotImplementedError in the "
    "file {module_path} so that the tests in {test_path} pass. Follow each "
    "docstring exactly. Use the edit tool with absolute paths. Do NOT modify "
    "the tests. Do not touch any other module. When done, reply DONE."
)

SOLO_BRIEF = (
    "Implement EVERY function currently raising NotImplementedError in these "
    "three files so that ALL the tests under {tests_dir} pass: {files}. "
    "Follow each docstring exactly. Use the edit tool with absolute paths. "
    "Do NOT modify the tests. When done, reply DONE."
)


def generate_project(out: Path, dup: bool) -> None:
    script = Path(__file__).parent / "demo_chat_corpus.py"
    cmd = [sys.executable, str(script), "--out", str(out)]
    if dup:
        cmd.append("--dup")
    subprocess.run(cmd, check=True)


def run_tests(project: Path) -> tuple[int, int]:
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
        cwd=project,
        capture_output=True,
        text=True,
    )
    output = proc.stderr + proc.stdout
    ran = re.search(r"Ran (\d+) tests", output)
    total = int(ran.group(1)) if ran else 0
    if proc.returncode == 0:
        return total, total
    failed = re.search(r"(?:failures=(\d+))", output)
    errors = re.search(r"(?:errors=(\d+))", output)
    bad = int(failed.group(1) if failed else 0) + int(
        errors.group(1) if errors else 0
    )
    return max(0, total - bad), total


def git(cwd: Path, *cmd: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.email=bench@meow.local",
            "-c",
            "user.name=Bench",
            *cmd,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def load_config() -> VibeConfig:
    return VibeConfig.load(
        session_logging=SessionLoggingConfig(enabled=False, save_dir="")
    )


def make_worker_loop(config: VibeConfig) -> AgentLoop:
    return AgentLoop(
        config_orchestrator=LegacyConfigOrchestrator(config),
        agent_name="worker",
        is_subagent=True,
        defer_heavy_init=True,
        headless=True,
        force_bypass_tool_permissions=True,
    )


async def run_agent(prompt: str, config: VibeConfig) -> tuple[int, int]:
    loop = await asyncio.to_thread(make_worker_loop, config)
    try:
        async with aclosing(loop.act(prompt)) as events:
            async for event in events:
                if isinstance(event, AssistantEvent):
                    pass
    finally:
        with suppress(Exception):
            await loop.aclose()
    return loop.stats.session_prompt_tokens, loop.stats.session_completion_tokens


async def bench_solo(project: Path, config: VibeConfig) -> dict[str, Any]:
    files = ", ".join(str(project / f"{m}.py") for m in project_modules(project))
    prompt = SOLO_BRIEF.format(tests_dir=project / "tests", files=files)
    start = time.monotonic()
    p_tokens, c_tokens = await run_agent(prompt, config)
    wall = time.monotonic() - start
    passed, total = run_tests(project)
    return {
        "name": "solo (1 agent)",
        "wall_s": round(wall, 1),
        "total_tokens": p_tokens + c_tokens,
        "agents": 1,
        "tests_passed": passed,
        "tests_total": total,
        "merge_conflicts": 0,
    }


async def bench_workers(project: Path, config: VibeConfig) -> dict[str, Any]:
    worktrees: list[tuple[str, Path]] = []
    for module in project_modules(project):
        wt_path = project.parent / f"wk_{module}"
        git(project, "worktree", "add", str(wt_path), "-b", f"wk_{module}")
        worktrees.append((module, wt_path))

    async def one(module: str, wt_path: Path) -> tuple[int, int]:
        prompt = WORKER_BRIEF.format(
            module_path=wt_path / f"{module}.py",
            test_path=wt_path / "tests" / f"test_{module}.py",
        )
        return await run_agent(prompt, config)

    start = time.monotonic()
    token_pairs = await asyncio.gather(
        *(one(module, wt) for module, wt in worktrees)
    )
    conflicts = 0
    for module, wt_path in worktrees:
        git(wt_path, "add", "-A")
        git(wt_path, "commit", "-m", f"worker: implement {module}")
        merge = git(project, "merge", "--no-edit", f"wk_{module}")
        if merge.returncode != 0:
            conflicts += 1
            git(project, "merge", "--abort")
    wall = time.monotonic() - start
    passed, total = run_tests(project)
    return {
        "name": "3 workers paralleles (worktrees)",
        "wall_s": round(wall, 1),
        "total_tokens": sum(p + c for p, c in token_pairs),
        "agents": len(worktrees),
        "tests_passed": passed,
        "tests_total": total,
        "merge_conflicts": conflicts,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="bench_chat")
    parser.add_argument("--dup", action="store_true")
    parsed = parser.parse_args()
    out_dir = Path(parsed.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    config = load_config()

    results: list[dict[str, Any]] = []

    solo_project = out_dir / "solo" / "project"
    generate_project(solo_project, parsed.dup)
    baseline_passed, baseline_total = run_tests(solo_project)
    print(f"etat initial: {baseline_passed}/{baseline_total} tests verts", flush=True)
    results.append(await bench_solo(solo_project, config))
    print(json.dumps(results[-1]), flush=True)

    workers_project = out_dir / "workers" / "project"
    generate_project(workers_project, parsed.dup)
    results.append(await bench_workers(workers_project, config))
    print(json.dumps(results[-1]), flush=True)

    (out_dir / "results.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    asyncio.run(main())
