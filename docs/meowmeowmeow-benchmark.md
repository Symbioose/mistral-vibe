# MeowMeowMeow benchmark — measured, live API

Real runs against the Mistral API (`mistral-medium-3.5`, explore subagents),
on a generated corpus with **planted ground truth**: semantic logic bugs
(inverted operators, off-by-one, skipped elements) that grep cannot find —
an agent must actually read and reason about every file. Recall/precision are
computed against the exact planted list, so the numbers below are verified,
not vibes.

Conditions per run:

- **baseline** — one explore agent told to audit every file (how a plain
  `task` call or chat turn would do it),
- **meow_meow_meow** — parallel fan-out, one agent per batch of files,
  schema-validated JSON findings, merged in script,
- **resume** — the same meow run re-invoked with `resume_from_run_id`.

## Small scale — 12 files (~40 lines each), 8 bugs

| condition | wall (s) | tokens | agents (api/cached) | bugs found | recall | precision |
|---|---|---|---|---|---|---|
| baseline (1 agent) | 12.2 | 15 423 | 1/0 | 8/8 | 100% | 100% |
| meow_meow_meow (6 agents) | 29.3 | 40 014 | 6/0 | 8/8 | 100% | 100% |
| meow resume (journal) | 0.0 | **0** | 0/6 | 8/8 | 100% | 100% |

At this size the whole corpus fits comfortably in one context: the single
agent wins on both time and tokens. **A workflow is the wrong tool for small
jobs** — the tool prompt says so, and this is the measured reason.

## Large scale — 36 files (~130 lines each), 18 bugs

| condition | wall (s) | tokens | agents (api/cached) | bugs found | recall | precision |
|---|---|---|---|---|---|---|
| baseline (1 agent) | 44.1 | 221 791 | 1/0 | 18/18 | 100% | 100% |
| meow_meow_meow (12 agents) | **28.2** | **115 307** | 12/0 | 18/18 | 100% | 100% |
| meow resume (journal) | **0.0** | **0** | 0/12 | 18/18 | 100% | 100% |

At 3× the corpus size the crossover has happened: the fan-out is **1.6×
faster** and uses **1.9× fewer tokens** at identical (perfect) accuracy.

## Maximum scale — 96 files (~130 lines each), 48 bugs

| condition | wall (s) | tokens | agents (api/cached) | bugs found | recall | precision |
|---|---|---|---|---|---|---|
| baseline (1 agent) | 469.6 | 1 717 909 | 1/0 | 48/48 | 100% | 100% |
| meow_meow_meow (32 agents) | **37.6** | **305 908** | 32/0 | 48/48 | 100% | 100% |
| meow resume (journal) | **0.0** | **0** | 0/32 | 48/48 | 100% | 100% |

**12.5× faster, 5.6× cheaper, identical perfect accuracy** (48/48, zero false
positives, verified against the exact planted list). The single agent spent
7 min 50 s and 1.72 M tokens on what the fan-out did in 38 seconds.

## The scaling law is the real result

| corpus | baseline tokens | baseline wall | meow tokens | meow wall |
|---|---|---|---|---|
| 12 files | 15 423 | 12.2 s | 40 014 | 29.3 s |
| 36 files | 221 791 | 44.1 s | 115 307 | 28.2 s |
| 96 files | 1 717 909 | 469.6 s | 305 908 | 37.6 s |
| **×8 files** | **×111** | **×38** | **×7.6** | **×1.3** |

The single agent re-sends its entire accumulated context on every tool turn,
so its token cost grows **super-linearly** with corpus size (×111 for ×8
corpus) and its wall-clock is sequential. The fan-out pays a fixed per-agent
overhead and then grows **linearly in tokens and near-flat in wall-clock**
(29 s → 38 s across an 8× corpus): each agent sees only its batch, and the
slowest batch bounds the run. The earlier extrapolation ("a 100-file audit
costs the baseline millions of tokens and the workflow ~300k") was measured
almost exactly at 96 files: 1.72 M vs 306 k.

## What the journal buys — measured

Re-running the identical workflow replayed all agents from the journal:
**0 API calls, 0 tokens, 0.0 s, identical 18/18 result.** In practice this is
the iteration story: when a workflow script crashes at phase 3, the fix
re-runs phases 1–2 for free. Script validation is the same story at the
front: an invalid script is rejected before any agent spawns, with every
violation listed — a failed attempt costs zero agent tokens.

## Reproduce

```
uv run python scripts/benchmark_meowmeowmeow.py --out bench_small
uv run python scripts/benchmark_meowmeowmeow.py --files 36 --bugs 18 --batch 3 --fillers 14 --out bench_large
uv run python scripts/benchmark_meowmeowmeow.py --files 96 --bugs 48 --batch 3 --fillers 14 --concurrency 16 --out bench_max
```

Single-run measurements (n=1 per condition) on one machine/network; treat
small deltas as noise, the ×5–×14 structural gaps as signal.
