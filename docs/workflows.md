# Workflows

Workflows let the agent orchestrate many subagents deterministically from a single
script — fan-out over items, pipeline stages, adversarial verification — with live
progress in the TUI and a journal that makes runs resumable.

The model invokes the `workflow` tool with a self-contained async Python script:

```python
meta = {
    "name": "review-changes",
    "description": "Review changed files across dimensions, verify each finding",
    "phases": [{"title": "Review"}, {"title": "Verify"}],
}

DIMENSIONS = [{"key": "bugs", "prompt": "..."}, {"key": "perf", "prompt": "..."}]

async def verify_stage(review, item, i):
    return await parallel([
        (lambda f=f: agent(f"Adversarially verify: {f['title']}", phase="Verify",
                           schema=VERDICT_SCHEMA))
        for f in review["findings"]
    ])

results = await pipeline(
    DIMENSIONS,
    lambda d: agent(d["prompt"], label=f"review:{d['key']}", phase="Review",
                    schema=FINDINGS_SCHEMA),
    verify_stage,
)
result({"confirmed": [f for g in results if g for f in g if f and f["is_real"]]})
```

## Script API

| Primitive | Semantics |
| --- | --- |
| `await agent(prompt, *, label, phase, schema, agent_name, model)` | Spawn a subagent. Returns final text (`str`), or the parsed JSON object when `schema` is given (validated, retried on mismatch). Returns `None` on terminal failure. |
| `await parallel(thunks)` | Run zero-arg callables concurrently. Barrier: waits for all. A failing thunk resolves to `None`, never raises. |
| `await pipeline(items, *stages)` | Each item flows through all stages independently — no barrier between stages. Stage signature `(prev, item, index)`; trailing params optional. A failing stage drops that item to `None`. |
| `phase(title)` | Start a display phase; later `agent()` calls group under it. |
| `log(message)` | Emit a narrator line in the TUI (`print` is aliased to this). |
| `result(value)` | Set the workflow's JSON return value (top-level `return` also works). |
| `args` | The value passed in the tool's `args` input, verbatim. |

## Guarantees and limits

- Scripts are validated before execution: `meta` must be a pure-literal dict,
  imports are rejected (`json`, `math`, `re` are pre-loaded), and wall-clock /
  randomness / filesystem access are unavailable (they would break resume).
- Concurrency is capped at `min(16, cpu - 2)` per workflow (configurable via
  `max_concurrency` in the tool config); excess `agent()` calls queue.
- Lifetime cap of 1000 `agent()` calls per workflow; a single `parallel()` /
  `pipeline()` accepts at most 4096 items.
- Structured output (`schema`) is validated with jsonschema and retried
  (`schema_retries`, default 2) with the validation error fed back.

## Resume

Every run persists `script.py`, `journal.jsonl`, and `result.json` under
`<session_dir>/workflows/<run_id>/`. Re-invoking with
`resume_from_run_id="wf_..."` replays successful `agent()` calls whose
`(prompt, schema, agent_name, model)` are unchanged straight from the journal —
only edited or previously-failed calls run live. Failed calls are never
journaled, so a resume retries exactly what needs retrying.

## Subagents

Each `agent()` call runs a fresh `AgentLoop` with a subagent profile
(`agent_name`, default `explore` — configurable via the tool config's
`default_agent`). `model` overrides `active_model` for that one call; the alias
must exist in the resolved model list. Subagent tool approvals bubble up through
the normal permission flow.

## TUI

The `workflow` tool renders a live tree in the chat: one group per phase, one
row per agent (spinner → ✓ / ✕ / □), durations, cached-replay markers, the last
few `log()` lines, and a running `done/total agents` counter in the header.
Finished rows beyond the last 6 per phase are folded into a `(+N earlier)`
count to keep long fan-outs compact.
