Execute a meow_meow_meow script that orchestrates multiple subagents deterministically.

A meow_meow_meow structures work across many agents — to be comprehensive (decompose and cover in parallel), to be confident (independent perspectives and adversarial checks before committing), or to take on scale one context can't hold (migrations, audits, broad sweeps). The script encodes that structure: what fans out, what verifies, what synthesizes.

## When to reach for this tool — decide yourself, don't wait for instructions

The user will NOT say "use a meow_meow_meow" or spell out phases. They will say things like
"audite ce repo", "trouve tous les bugs", "comprends cette codebase", "vérifie que
tout est cohérent", "compare ces approches", "migre tous les usages de X". YOUR job
is to recognize that the ask is broad, deep, or repetitive, and to design the
decomposition yourself:

- Broad ask ("understand/audit/review X") → decompose X into its natural parts
  (directories, subsystems, dimensions like correctness/security/perf), one agent
  per part, then a synthesis stage.
- Unknown-size discovery ("find all the bugs/usages/issues") → loop-until-dry with
  parallel finders, dedup in plain code, adversarial verification of each finding.
- High-stakes conclusion → never trust one agent's claim; spawn independent
  skeptics with distinct lenses and keep only what survives.
- Repetitive transformation ("do X for every Y") → enumerate the Ys first (one
  scout agent or a cheap deterministic pass), then pipeline over them.

Default to MORE structure, not less: a real meow_meow_meow has 2–4 phases (scout →
fan-out → verify → synthesize), tens of agents when the target is large, and
explicit verification before reporting. A single agent() call wrapped in a
meow_meow_meow is a waste — use the `task` tool for that. Prompts you give each agent
must be self-contained briefs: context, exact question, expected output shape —
the agent knows nothing except what you write.

Briefs must name CONCRETE FILE PATHS, never just topics. "Audit vibe/core" produces
answers from general knowledge; "Read vibe/core/agent_loop/_loop.py and
vibe/core/session/*.py, then report ..." produces grounded findings. Use a scout
agent (or a cheap ls/glob pass) in phase 1 to enumerate real paths, then embed
those paths in every phase-2 brief. Require evidence in the output schema
(e.g. a "file" and "lines" field per finding) so ungrounded claims are visible.

## Script format

Scripts are plain **async Python** (top-level `await` is allowed; type annotations are fine). Every script must begin with a `meta` assignment — a PURE LITERAL dict (no variables, calls, or comprehensions):

```python
meta = {
    "name": "find-flaky-tests",
    "description": "Find flaky tests and propose fixes",   # one line, shown to the user
    "phases": [                                            # optional, one entry per phase() call
        {"title": "Scan", "detail": "grep test logs for retries"},
        {"title": "Fix", "detail": "one agent per flaky test"},
    ],
}

phase("Scan")
flaky = await agent("grep CI logs for retry markers", schema=FLAKY_SCHEMA)
...
return final_value   # top-level return = what the meow_meow_meow returns
```

Required meta fields: `name`, `description`. Optional: `phases`. Use the SAME phase titles in `meta["phases"]` as in `phase()` calls — titles are matched exactly.

## Script API

- `await agent(prompt, *, label=None, phase=None, schema=None, agent_name=None, model=None)` — spawn a subagent. Returns its final text as a `str`. With `schema` (a JSON Schema dict), the subagent must produce JSON matching it and the call returns the parsed object — no parsing needed; validation failures are retried automatically. Returns `None` if the subagent dies on a terminal error (filter with `[r for r in results if r is not None]`). `label` overrides the display label; `phase` assigns the agent to a progress group (use it inside `pipeline()`/`parallel()` stages to avoid races on the global `phase()` state). `agent_name` selects a configured subagent profile; omit it to use the default. `model` overrides the model for this one call — default to omitting it.
- `await parallel(thunks)` — run zero-arg callables concurrently. This is a BARRIER: awaits all before returning. A thunk that raises resolves to `None` in the result list — the call itself never raises.
- `await pipeline(items, *stages)` — run each item through all stages independently, NO barrier between stages: item A can be in stage 3 while item B is still in stage 1. Each stage callable receives `(prev_result, original_item, index)` — extra trailing parameters are optional. A stage that raises drops that item to `None` and skips its remaining stages.
- `phase(title)` — start a new phase; subsequent `agent()` calls are grouped under this title in the progress display.
- `log(message)` — emit a progress line to the user.
- `return value` — a top-level return ends the script and sets the meow_meow_meow's return value (JSON-serializable). `result(value)` exists as an alias but prefer `return`.
- `args` — the value passed as the tool's `args` input, verbatim (`None` if not provided). Use it to parameterize meowmeowmeow instead of hardcoding.
- `prompts` — dict of the tool's `prompts` input. THIS IS WHERE AGENT PROMPTS LIVE: pass every multi-line or prose-heavy agent brief in the `prompts` tool argument (JSON handles quoting/newlines safely) and reference it as `prompts["key"]`, optionally composing per-item context: `agent(prompts["review"] + "\n\nFile: " + path)`. Embedding long prose directly in Python strings is the #1 cause of syntax errors — keep the script mechanical, keep the prose in JSON.

RESERVED NAMES: `agent`, `parallel`, `pipeline`, `phase`, `log`, `result`, `args` are primitives — the script is REJECTED at parse time if any of them is used as a variable, parameter, or function name (e.g. `for result in ...` is invalid; use `for verdict in ...`).

VALIDATION CONTRACT: every script is statically validated before a single agent runs — syntax, meta shape, reserved names, imports, forbidden modules, and missing `await` on `agent`/`parallel`/`pipeline` calls. A rejected script costs nothing; the error lists ALL violations with line numbers. Fix every listed item and re-invoke once — do not fix them one at a time.

HARD CAPS (rejected, not warned): scripts over 200 lines, and any string literal over 250 characters. These exist because prose belongs in the `prompts` tool argument, not in Python strings. Write the script FIRST as short mechanical code (typically 20–60 lines), with every agent brief as `prompts["key"]` — then put the actual briefs in `prompts`.

Subagents are told their final text IS the return value (not a human-facing message), so they return raw data. For structured output, use `schema` — validation happens at the call layer and the model retries on mismatch.

## Rules

- DEFAULT TO `pipeline()`. Only use a barrier (`parallel` between stages) when stage N genuinely needs cross-item context from ALL of stage N-1 (dedup/merge across the full set, early-exit on zero findings, prompts that reference "the other findings"). "I need to flatten/filter first" is NOT a reason — do it inside a pipeline stage.
- Concurrent `agent()` calls are capped per meow_meow_meow — excess calls queue and run as slots free up. You can pass 100 items; they all complete. Total agent count per meow_meow_meow is capped at 1000; a single `parallel()`/`pipeline()` call accepts at most 4096 items.
- `time.time()`, `datetime.now()`, `random`, filesystem and network access are unavailable inside scripts (they would break resume) — pass timestamps and randomness in via `args`; subagents do the real-world work.
- If a meow_meow_meow bounds coverage (top-N, sampling), `log()` what was dropped — silent truncation reads as "covered everything" when it didn't.
- Each agent's inner tool activity already streams live to the user — use `log()` for meow_meow_meow-level milestones (phase transitions, counts, decisions), not to narrate individual agents.
- NEVER index into an agent's result (`out["key"]`, `out[0]`) unless that call used `schema=` — without a schema the result is free text. And ALWAYS guard for `None` before indexing: failed agents return `None` (`[o for o in outs if o]`).
- NEVER await agents one at a time in a loop (`for x in items: await agent(...)`) — that serializes everything and wastes wall-clock. Within a phase, ALWAYS fan out with `parallel([...])` or `pipeline(...)`; sequential awaits are only for genuine dependencies BETWEEN phases. Two `await agent(...)` statements in a row with no data dependency between them are a bug: wrap them in one `parallel([...])`.
- SHARD heavy briefs. One agent covering a whole codebase or >~10 files will run for many minutes and read shallowly. Split it into disjoint slices (`scan:core`, `scan:cli`, `scan:tests`… or by file batch) run in ONE `parallel([...])`, and merge in code. An agent expected to run over ~2 minutes is a decomposition failure, not thoroughness.
- MODEL TIERING: the global `fast_model` holds the alias of a faster configured model (or `None` if the user has not set one). For mechanical shard work (scanning, enumeration, extraction) pass `model=fast_model` when it is set; keep the default model for judgment work (verification, synthesis, adversarial review).
- ALWAYS declare `meta["phases"]` (one entry per `phase()` call, with a short `detail`): the UI shows the full plan upfront and tracks each phase live.

## Patterns

Canonical multi-stage review — pipeline by default, each dimension verifies as soon as its review completes. Note the tool call carries the prose in `prompts`; the script only references it:

```json
"prompts": {
  "review:bugs": "You are reviewing <diff summary...>. Hunt for correctness bugs: ...",
  "review:perf": "Same diff. Hunt for performance regressions: ...",
  "verify": "A reviewer claims the following finding. Try hard to REFUTE it. Finding: "
}
```

```python
meta = {"name": "review-changes", "description": "Review changed files, verify findings",
        "phases": [{"title": "Review"}, {"title": "Verify"}]}
DIMENSIONS = ["review:bugs", "review:perf"]

async def verify_stage(review, item, i):
    return await parallel([
        (lambda f=f: agent(prompts["verify"] + json.dumps(f), label="verify:" + f["file"],
                           phase="Verify", schema=VERDICT_SCHEMA))
        for f in review["findings"]
    ])

results = await pipeline(
    DIMENSIONS,
    lambda key: agent(prompts[key], label=key, phase="Review", schema=FINDINGS_SCHEMA),
    verify_stage,
)
confirmed = [f for group in results if group for f in group if f and f.get("is_real")]
return {"confirmed": confirmed}
```

Loop-until-dry — for unknown-size discovery, keep spawning finders until 2 consecutive rounds return nothing new; dedup against everything SEEN (not just confirmed), or judge-rejected findings reappear every round:

```python
seen, confirmed, dry = set(), [], 0
finder_keys = ["find:logic", "find:concurrency", "find:edge-cases"]  # prose in `prompts`
while dry < 2:
    found = await parallel([(lambda k=k: agent(prompts[k], phase="Find", schema=BUGS)) for k in finder_keys])
    fresh = [b for r in found if r for b in r["bugs"] if key(b) not in seen]
    if not fresh:
        dry += 1
        continue
    dry = 0
    seen.update(key(b) for b in fresh)
    votes = await parallel([(lambda b=b: agent(f"Try to refute: {b['desc']}", phase="Verify", schema=VERDICT))
                            for b in fresh])
    confirmed += [b for b, v in zip(fresh, votes) if v and not v["refuted"]]
return confirmed
```

Quality patterns — compose freely: adversarial verify (N independent skeptics per finding, kill if the majority refute), perspective-diverse verify (distinct lenses instead of N identical refuters), judge panel (N independent attempts, parallel judges, synthesize from the winner), multi-modal sweep (parallel agents each searching a different way), completeness critic (a final agent asking "what's missing?").

Scale to what the user asked for: "find any bugs" → a few finders, single-vote verify; "thoroughly audit this" → larger finder pool, 3–5 vote adversarial pass, synthesis stage.
