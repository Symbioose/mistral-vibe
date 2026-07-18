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

## The scaling law is the real result

Going from 12 to 36 files:

| | baseline tokens | meow tokens |
|---|---|---|
| 12 files | 15 423 | 40 014 |
| 36 files | 221 791 (**×14.4**) | 115 307 (**×2.9**) |

The single agent re-sends its entire accumulated context on every tool turn,
so its token cost grows **super-linearly** with corpus size (and eventually
hits the context ceiling and compaction, which silently drops information).
The fan-out pays a fixed per-agent overhead (~2k system prompt) and then
grows **linearly** — each agent sees only its batch. Extrapolating the
measured trend, a 100-file audit costs the baseline millions of tokens (if
it survives at all) and the workflow roughly 300k.

Wall-clock follows the same shape: the baseline reads files sequentially
(O(n)), the fan-out is bounded by its slowest batch (O(n / concurrency),
cap = min(16, cores − 2)).

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
```

Single-run measurements (n=1 per condition) on one machine/network; treat
small deltas as noise, the ×5–×14 structural gaps as signal.
