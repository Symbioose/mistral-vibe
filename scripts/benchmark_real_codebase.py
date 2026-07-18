"""Benchmark on REAL code: planted single-line mutations in the `rich` library.

Copies real source files from the installed `rich` package, flips one
operator/comparison per selected file (classic mutation testing), and asks
(a) one solo agent and (b) a MeowMeowMeow parallel fan-out to find the broken
lines. Ground truth is the exact mutation list; a finding matches when it
names the right file within +/-3 lines.

Usage: uv run python scripts/benchmark_real_codebase.py --out DIR [--skip-baseline]
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
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

N_FILES = 40
N_MUTATIONS = 20
BATCH_SIZE = 4
MIN_LINES = 40
MAX_LINES = 400
LINE_TOLERANCE = 3

MUTATION_RULES: list[tuple[str, str]] = [
    (" <= ", " < "),
    (" >= ", " > "),
    (" == ", " != "),
    (" is not None", " is None"),
    (" + 1", " - 1"),
    (" and ", " or "),
]

FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "bugs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "why": {"type": "string"},
                },
                "required": ["file", "line"],
            },
        }
    },
    "required": ["bugs"],
}

REVIEW_BRIEF = (
    "The files below are real source files from a production Python library, "
    "but SOME lines have been sabotaged with a single-token logic flip "
    "(inverted comparison, and/or swap, off-by-one, is/is-not None). Read "
    "each file completely and reason about intent vs code. Report ONLY lines "
    "you are confident were sabotaged — the surrounding code is correct "
    'production code. Respond ONLY with JSON {"bugs": [{"file": '
    '"<basename>", "line": <int>, "why": "..."}]}.\n\nFiles:\n'
)


def _is_code_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    if '"' in line or "'" in line:
        return False
    return line.startswith((" ", "\t"))


def build_corpus(out: Path) -> tuple[list[Path], list[dict[str, Any]]]:
    import rich

    src_dir = Path(rich.__file__).parent
    candidates = []
    for path in sorted(src_dir.glob("*.py")):
        n_lines = len(path.read_text(encoding="utf-8").splitlines())
        if MIN_LINES <= n_lines <= MAX_LINES:
            candidates.append(path)
    selected = candidates[:N_FILES]
    if len(selected) < N_FILES:
        raise SystemExit(f"only {len(selected)} candidate files in rich")

    corpus = out / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    truth: list[dict[str, Any]] = []
    mutate_every = max(1, len(selected) // N_MUTATIONS)
    for index, src in enumerate(selected):
        lines = src.read_text(encoding="utf-8").splitlines()
        if index % mutate_every == 0 and len(truth) < N_MUTATIONS:
            planted = _plant_mutation(lines, len(truth))
            if planted is not None:
                lineno, before, after = planted
                truth.append(
                    {
                        "file": src.name,
                        "line": lineno,
                        "before": before.strip(),
                        "after": after.strip(),
                    }
                )
        (corpus / src.name).write_text(
            "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
        )
    return sorted(corpus.glob("*.py")), truth


def _plant_mutation(lines: list[str], salt: int) -> tuple[int, str, str] | None:
    rules = MUTATION_RULES[salt % len(MUTATION_RULES) :] + MUTATION_RULES[
        : salt % len(MUTATION_RULES)
    ]
    for old, new in rules:
        for i, line in enumerate(lines):
            if _is_code_line(line) and old in line:
                before = line
                lines[i] = line.replace(old, new, 1)
                return i + 1, before, lines[i]
    return None


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


async def run_single_agent(prompt: str, config: VibeConfig) -> tuple[str, int, int]:
    loop = await asyncio.to_thread(make_loop, config)
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


@dataclass
class RunMetrics:
    name: str
    truth: list[dict[str, Any]]
    wall_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    api_agents: int = 0
    cached_agents: int = 0
    found: list[tuple[str, int]] = field(default_factory=list)

    def score(self) -> dict[str, Any]:
        truth_pairs = [(t["file"], int(t["line"])) for t in self.truth]
        matched: set[int] = set()
        hits = 0
        for file_name, line in self.found:
            for t_index, (t_file, t_line) in enumerate(truth_pairs):
                if (
                    t_index not in matched
                    and t_file == file_name
                    and abs(t_line - line) <= LINE_TOLERANCE
                ):
                    matched.add(t_index)
                    hits += 1
                    break
        recall = hits / len(truth_pairs) if truth_pairs else 0.0
        precision = hits / len(self.found) if self.found else 0.0
        return {
            "name": self.name,
            "wall_s": round(self.wall_s, 1),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "api_agents": self.api_agents,
            "cached_agents": self.cached_agents,
            "bugs_found": hits,
            "bugs_total": len(truth_pairs),
            "reports": len(self.found),
            "false_positives": len(self.found) - hits,
            "recall": round(recall, 3),
            "precision": round(precision, 3),
        }


def parse_findings(raw: Any) -> list[tuple[str, int]]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw, err = parse_structured(raw, FINDINGS_SCHEMA)
        if err is not None:
            return []
    pairs = []
    for bug in raw.get("bugs", []):
        with suppress(TypeError, ValueError):
            pairs.append((Path(str(bug.get("file", ""))).name, int(bug["line"])))
    return sorted(set(pairs))


async def bench_baseline(
    files: list[Path], config: VibeConfig, truth: list[dict[str, Any]]
) -> RunMetrics:
    metrics = RunMetrics("solo (1 agent)", truth)
    prompt = REVIEW_BRIEF + "\n".join(str(p) for p in files)
    start = time.monotonic()
    text, p_tokens, c_tokens = await run_single_agent(prompt, config)
    metrics.wall_s = time.monotonic() - start
    metrics.prompt_tokens = p_tokens
    metrics.completion_tokens = c_tokens
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
        text, p_tokens, c_tokens = await run_single_agent(request.prompt, self._config)
        self.prompt_tokens += p_tokens
        self.completion_tokens += c_tokens
        return SubagentOutcome(success=True, text=text)


MEOW_SCRIPT = """
meta = {
    "name": "bench-real-audit",
    "description": "Parallel mutation hunt in real library code",
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
    out_dir: Path,
    truth: list[dict[str, Any]],
    *,
    resume: bool,
) -> RunMetrics:
    name = "meow resume (journal)" if resume else "meow (parallel)"
    metrics = RunMetrics(name, truth)
    batches = [
        "\n".join(str(p) for p in files[i : i + BATCH_SIZE])
        for i in range(0, len(files), BATCH_SIZE)
    ]
    spawner = BenchSpawner(config)
    journal = MeowMeowMeowJournal.create(
        out_dir / ("resume.jsonl" if resume else "first.jsonl"),
        resume_from=out_dir / "first.jsonl" if resume else None,
    )
    runtime = MeowMeowMeowRuntime(
        parse_meow_meow_meow_script(MEOW_SCRIPT),
        spawner,
        args={"batches": batches, "schema": FINDINGS_SCHEMA},
        prompts={"review": REVIEW_BRIEF},
        journal=journal,
        max_concurrency=16,
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


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="bench_real")
    parser.add_argument("--skip-baseline", action="store_true")
    parsed_args = parser.parse_args()
    out_dir = Path(parsed_args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    files, truth = build_corpus(out_dir)
    (out_dir / "ground_truth.json").write_text(
        json.dumps(truth, indent=2), encoding="utf-8"
    )
    total_lines = sum(
        len(p.read_text(encoding="utf-8").splitlines()) for p in files
    )
    print(
        f"corpus: {len(files)} real files from rich, {total_lines} lines, "
        f"{len(truth)} mutations",
        flush=True,
    )

    config = load_config()
    results: list[dict[str, Any]] = []
    if not parsed_args.skip_baseline:
        baseline = await bench_baseline(files, config, truth)
        results.append(baseline.score())
        print(json.dumps(results[-1]), flush=True)

    meow = await bench_meow(files, config, out_dir, truth, resume=False)
    results.append(meow.score())
    print(json.dumps(results[-1]), flush=True)

    replay = await bench_meow(files, config, out_dir, truth, resume=True)
    results.append(replay.score())
    print(json.dumps(results[-1]), flush=True)

    (out_dir / "results.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    asyncio.run(main())
