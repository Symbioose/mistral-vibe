"""Benchmark: single-agent baseline vs MeowMeowMeow fan-out on a ground-truth task.

Generates a corpus of Python files with planted logic bugs (invisible to grep),
then measures wall-clock, tokens, and recall/precision for:
  A. baseline  — one explore agent reads everything sequentially
  B. meow      — parallel fan-out, one agent per file batch, schema-validated
  C. resume    — run B again from its journal (replay, no API calls)

Usage: uv run python scripts/benchmark_meowmeowmeow.py [--out DIR]
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
import json
from pathlib import Path
import sys
import time
from typing import Any

from vibe.core.config.harness_files import init_harness_files_manager

init_harness_files_manager()

from vibe.core.agent_loop import AgentLoop
from vibe.core.config import SessionLoggingConfig, VibeConfig
from vibe.core.config.orchestrator_legacy import LegacyConfigOrchestrator
from vibe.core.meowmeowmeow.journal import MeowMeowMeowJournal
from vibe.core.meowmeowmeow.models import SubagentOutcome, SubagentRequest
from vibe.core.meowmeowmeow.runtime import MeowMeowMeowRuntime
from vibe.core.meowmeowmeow.script import parse_meow_meow_meow_script
from vibe.core.meowmeowmeow.structured import parse_structured
from vibe.core.types import AssistantEvent

BATCH_SIZE = 2

OK_FUNCTIONS = {
    "mean": (
        "def mean(values: list[float]) -> float:\n"
        "    if not values:\n"
        "        raise ValueError('empty')\n"
        "    return sum(values) / len(values)\n"
    ),
    "clamp": (
        "def clamp(value: int, low: int, high: int) -> int:\n"
        "    return max(low, min(high, value))\n"
    ),
    "count_evens": (
        "def count_evens(items: list[int]) -> int:\n"
        "    return sum(1 for item in items if item % 2 == 0)\n"
    ),
    "join_nonempty": (
        "def join_nonempty(parts: list[str], sep: str) -> str:\n"
        "    return sep.join(p for p in parts if p)\n"
    ),
    "running_total": (
        "def running_total(items: list[int]) -> list[int]:\n"
        "    totals, acc = [], 0\n"
        "    for item in items:\n"
        "        acc += item\n"
        "        totals.append(acc)\n"
        "    return totals\n"
    ),
    "last_index_of": (
        "def last_index_of(items: list[int], needle: int) -> int:\n"
        "    for i in range(len(items) - 1, -1, -1):\n"
        "        if items[i] == needle:\n"
        "            return i\n"
        "    return -1\n"
    ),
}

BUGGY_FUNCTIONS = {
    "mean": (
        "def mean(values: list[float]) -> float:\n"
        "    if not values:\n"
        "        raise ValueError('empty')\n"
        "    return sum(values) / (len(values) - 1)\n"
    ),
    "clamp": (
        "def clamp(value: int, low: int, high: int) -> int:\n"
        "    return min(low, max(high, value))\n"
    ),
    "count_evens": (
        "def count_evens(items: list[int]) -> int:\n"
        "    return sum(1 for item in items if item % 2 == 1)\n"
    ),
    "join_nonempty": (
        "def join_nonempty(parts: list[str], sep: str) -> str:\n"
        "    return sep.join(p for p in parts)\n"
    ),
    "running_total": (
        "def running_total(items: list[int]) -> list[int]:\n"
        "    totals, acc = [], 0\n"
        "    for item in items:\n"
        "        totals.append(acc)\n"
        "        acc += item\n"
        "    return totals\n"
    ),
    "last_index_of": (
        "def last_index_of(items: list[int], needle: int) -> int:\n"
        "    for i in range(len(items)):\n"
        "        if items[i] == needle:\n"
        "            return i\n"
        "    return -1\n"
    ),
}

FUNCTION_ORDER = list(OK_FUNCTIONS)

# (file_index, function_name) — 8 bugs across 12 files, none greppable.
PLANTED_BUGS: list[tuple[int, str]] = [
    (0, "mean"),
    (2, "running_total"),
    (3, "clamp"),
    (5, "last_index_of"),
    (6, "count_evens"),
    (8, "join_nonempty"),
    (9, "mean"),
    (11, "running_total"),
]
N_FILES = 12

FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "bugs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "function": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["file", "function"],
            },
        }
    },
    "required": ["bugs"],
}

REVIEW_BRIEF = (
    "You are auditing utility modules for LOGIC bugs (wrong operator, "
    "off-by-one, inverted condition, skipped element). Read EVERY function of "
    "EVERY file listed below completely. Only report functions whose behavior "
    "is actually wrong; do not report style issues. Return ONLY JSON matching "
    '{"bugs": [{"file": "<basename>", "function": "<name>", "description": '
    '"<why>"}]}.\n\nFiles to audit:\n'
)


def build_corpus(root: Path) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    planted = set(PLANTED_BUGS)
    paths: list[Path] = []
    for index in range(N_FILES):
        blocks = [f'"""Utility module m{index:02d}."""\n']
        for name in FUNCTION_ORDER:
            source = (
                BUGGY_FUNCTIONS[name]
                if (index, name) in planted
                else OK_FUNCTIONS[name]
            )
            blocks.append(source)
        path = root / f"m{index:02d}.py"
        path.write_text("\n\n".join(blocks), encoding="utf-8")
        paths.append(path)
    return paths


def load_config() -> VibeConfig:
    return VibeConfig.load(
        session_logging=SessionLoggingConfig(enabled=False, save_dir="")
    )


def make_loop(config: VibeConfig) -> AgentLoop:
    return AgentLoop(
        config_orchestrator=LegacyConfigOrchestrator(config),
        agent_name="explore",
        is_subagent=True,
        defer_heavy_init=True,
        headless=True,
        force_bypass_tool_permissions=True,
    )


@dataclass
class RunMetrics:
    name: str
    wall_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    api_agents: int = 0
    cached_agents: int = 0
    found: list[tuple[str, str]] = field(default_factory=list)

    def score(self) -> dict[str, Any]:
        truth = {(f"m{i:02d}.py", fn) for i, fn in PLANTED_BUGS}
        found = set(self.found)
        hits = truth & found
        recall = len(hits) / len(truth)
        precision = len(hits) / len(found) if found else 0.0
        return {
            "name": self.name,
            "wall_s": round(self.wall_s, 1),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "api_agents": self.api_agents,
            "cached_agents": self.cached_agents,
            "bugs_found": len(hits),
            "bugs_total": len(truth),
            "false_positives": len(found - truth),
            "recall": round(recall, 3),
            "precision": round(precision, 3),
        }


def parse_findings(raw: Any) -> list[tuple[str, str]]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw, err = parse_structured(raw, FINDINGS_SCHEMA)
        if err is not None:
            return []
    pairs = []
    for bug in raw.get("bugs", []):
        file_name = Path(str(bug.get("file", ""))).name
        pairs.append((file_name, str(bug.get("function", ""))))
    return sorted(set(pairs))


async def run_single_agent(prompt: str, config: VibeConfig) -> tuple[str, int, int]:
    loop = make_loop(config)
    final: list[str] = []
    current_id: str | None = None
    try:
        async with aclosing(loop.act(prompt)) as events:
            async for event in events:
                if isinstance(event, AssistantEvent) and event.content:
                    if event.message_id is not None and event.message_id != current_id:
                        current_id = event.message_id
                        final.clear()
                    final.append(event.content)
    finally:
        with suppress(Exception):
            await loop.aclose()
    stats = loop.stats
    return (
        "".join(final).strip(),
        stats.session_prompt_tokens,
        stats.session_completion_tokens,
    )


async def bench_baseline(files: list[Path], config: VibeConfig) -> RunMetrics:
    metrics = RunMetrics("baseline (1 agent)")
    listing = "\n".join(str(p) for p in files)
    prompt = REVIEW_BRIEF + listing
    start = time.monotonic()
    text, prompt_tokens, completion_tokens = await run_single_agent(prompt, config)
    metrics.wall_s = time.monotonic() - start
    metrics.prompt_tokens = prompt_tokens
    metrics.completion_tokens = completion_tokens
    metrics.api_agents = 1
    parsed, err = parse_structured(text, FINDINGS_SCHEMA)
    metrics.found = parse_findings(parsed if err is None else text)
    return metrics


class BenchSpawner:
    def __init__(self, config: VibeConfig) -> None:
        self._config = config
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.api_calls = 0

    async def run(
        self, request: SubagentRequest, on_progress: Callable[[str], None]
    ) -> SubagentOutcome:
        self.api_calls += 1
        text, prompt_tokens, completion_tokens = await run_single_agent(
            request.prompt, self._config
        )
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        return SubagentOutcome(success=True, text=text)


MEOW_SCRIPT = """
meta = {
    "name": "bench-audit",
    "description": "Parallel bug audit of the benchmark corpus",
    "phases": [{"title": "Audit"}],
}
phase("Audit")
outs = await parallel([
    (lambda batch=batch: agent(prompts["review"] + batch, schema=args["schema"]))
    for batch in args["batches"]
])
merged = []
for out in outs:
    if out:
        merged.extend(out.get("bugs", []))
return {"bugs": merged}
"""


async def bench_meow(
    files: list[Path],
    config: VibeConfig,
    journal_dir: Path,
    *,
    resume: bool,
) -> RunMetrics:
    name = "meow resume (journal)" if resume else "meow_meow_meow (parallel)"
    metrics = RunMetrics(name)
    batches = [
        "\n".join(str(p) for p in files[i : i + BATCH_SIZE])
        for i in range(0, len(files), BATCH_SIZE)
    ]
    spawner = BenchSpawner(config)
    journal_path = journal_dir / ("resume.jsonl" if resume else "first.jsonl")
    journal = MeowMeowMeowJournal.create(
        journal_path,
        resume_from=journal_dir / "first.jsonl" if resume else None,
    )
    runtime = MeowMeowMeowRuntime(
        parse_meow_meow_meow_script(MEOW_SCRIPT),
        spawner,
        args={"batches": batches, "schema": FINDINGS_SCHEMA},
        prompts={"review": REVIEW_BRIEF},
        journal=journal,
    )
    start = time.monotonic()
    outcome = await runtime.run()
    metrics.wall_s = time.monotonic() - start
    metrics.prompt_tokens = spawner.prompt_tokens
    metrics.completion_tokens = spawner.completion_tokens
    metrics.api_agents = spawner.api_calls
    metrics.cached_agents = outcome.agents_cached
    metrics.found = parse_findings(outcome.value)
    return metrics


def render_report(rows: list[dict[str, Any]], out: Path) -> str:
    header = (
        "| condition | wall (s) | tokens | agents (api/cached) | bugs found | "
        "false pos. | recall | precision |\n"
        "|---|---|---|---|---|---|---|---|\n"
    )
    lines = [
        f"| {r['name']} | {r['wall_s']} | {r['total_tokens']} | "
        f"{r['api_agents']}/{r['cached_agents']} | "
        f"{r['bugs_found']}/{r['bugs_total']} | {r['false_positives']} | "
        f"{r['recall']:.0%} | {r['precision']:.0%} |"
        for r in rows
    ]
    report = header + "\n".join(lines) + "\n"
    out.write_text(report, encoding="utf-8")
    return report


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="bench_out")
    parsed_args = parser.parse_args()
    out_dir = Path(parsed_args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    corpus_dir = out_dir / "corpus"
    files = build_corpus(corpus_dir)
    config = load_config()
    print(f"corpus: {len(files)} files, {len(PLANTED_BUGS)} planted bugs", flush=True)

    results: list[dict[str, Any]] = []
    baseline = await bench_baseline(files, config)
    results.append(baseline.score())
    print(json.dumps(results[-1]), flush=True)

    meow = await bench_meow(files, config, out_dir, resume=False)
    results.append(meow.score())
    print(json.dumps(results[-1]), flush=True)

    replay = await bench_meow(files, config, out_dir, resume=True)
    results.append(replay.score())
    print(json.dumps(results[-1]), flush=True)

    (out_dir / "results.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8"
    )
    print(render_report(results, out_dir / "report.md"), flush=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
