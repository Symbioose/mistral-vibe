# Parallel Write-Capable Subagents ("Workers") — Specification

Status: proposed (spec only, no implementation).
Scope: unlock Claude Code-style swarms of subagents that *implement code concurrently*, safely, with a fully attributable event stream. The declarative workflow layer and the live visualization are consumers of this design, not part of it.

---

## 1. Verified codebase map

Every claim below was verified directly against the source. Where the prior quick analysis was wrong or incomplete, it is called out inline.

### 1.1 H1 — Tool concurrency: CONFIRMED (concurrency is NOT the blocker)

Tool calls within one assistant turn already run concurrently. `AgentLoop._handle_tool_calls` emits one `ToolCallEvent` per call, then delegates to `_run_tools_concurrently`, which fans every call out as its own `asyncio.create_task` and multiplexes events back through an `asyncio.Queue` (`vibe/core/agent_loop/_loop.py:1614-1631`, `1651-1693`, `1695-1704`).

Because the `task` tool is just another tool, **two `task` calls in one turn already run in parallel today** — each builds a nested `AgentLoop` and drives it via `subagent_loop.act(...)` (`vibe/core/tools/builtins/task.py:126-134`, `152`). The task prompt even encourages fan-out: "Launch multiple subagents in parallel for independent work" (`vibe/core/tools/builtins/prompts/task.md:7`).

Cancellation of the fan-out is also already correct: parent cancellation / generator close cancels all in-flight tool tasks (`_loop.py:1678-1693`), each tool converts `CancelledError` into a `ToolResultEvent(cancelled=True)` and re-raises (`_loop.py:1773-1794`), and the task tool always closes its child loop in a `finally` (`task.py:181-183`).

### 1.2 H2 — "Read-only by design": CONFIRMED, but it is *policy*, not mechanism

Three thin layers make subagents read-only; none is a hard architectural constraint:

1. The only builtin `SUBAGENT` profile is `EXPLORE`, with `enabled_tools: ["grep", "read_file"]` (`vibe/core/agents/models.py:142-149`).
2. `TaskToolConfig.allowlist` defaults to `[BuiltinAgentName.EXPLORE]` (`task.py:51-53`); non-allowlisted agents fall back to ASK (`task.py:87-98`).
3. The tool prompt states "Subagents run read-only: they cannot modify files or ask the user questions" (`prompts/task.md:9`).

What the prior analysis **missed**: a user can *already* define a write-capable subagent via a custom TOML profile (`AgentProfile.from_toml`, `models.py:71-84`; discovery from `~/.vibe/agents/` and `.vibe/agents/`, `vibe/core/agents/manager.py:101-132`) — and the parent's `approval_callback` and `PermissionStore` are *already* propagated into the child loop (`task.py:132`, `138-139`). So a subagent hitting ASK **can** ask the user today; with no callback it is silently denied ("Tool execution not permitted.", `_loop.py:1954-1959`). The real blockers are the items below, not the permission plumbing.

Recursion guard: only `AgentType.SUBAGENT` profiles can be spawned by `task` (`task.py:113-118`), and subagent profiles can't be selected as the primary agent (`manager.py:52-57`). Since a subagent profile would need `task` in its `enabled_tools` to recurse, and no builtin subagent has it, depth is effectively 1.

### 1.3 H3 — Event identity: CONFIRMED (no attribution exists)

No event class in `vibe/core/types.py:461-563` (nor hook events in `vibe/core/hooks/models.py`, teleport events in `vibe/core/teleport/types.py`) carries any agent identity — no id, parent, name, role, or depth. The only hierarchy signal anywhere is `AgentLoop.parent_session_id` / `SessionMetadata.parent_session_id` (`types.py:152-154`), which never reaches the event stream.

Worse, the task tool **swallows** the child's stream: it accumulates `AssistantEvent` text, converts child `ToolResultEvent`s into flattened `ToolStreamEvent` one-liners under the *parent's* `tool_call_id`, and silently drops everything else (reasoning, tool calls, streams) (`task.py:149-169`). The Textual UI (`vibe/cli/textual_ui/handlers/event_handler.py:145-190`) and the ACP bridge (`vibe/acp/acp_agent_loop.py:1708-1796`) therefore have zero subagent awareness. Parallel child streams cannot be attributed or rendered today.

### 1.4 H4 — Worktree machinery: CONFIRMED (exists, complete, wired only to the CLI flag)

`vibe/core/worktree.py` is a full lifecycle: `prepare_worktree_session` creates/reuses a worktree + branch under `$VIBE_HOME/worktrees/<repo>-<hash>/<name>` (`worktree.py:62-106`, `182-185`; `vibe/core/paths/_vibe_home.py:31`), `inspect_worktree_for_cleanup` reports dirty state (`worktree.py:133-151`), `remove_worktree` tears down (`worktree.py:154-162`). It is wired exclusively to the `--worktree` CLI flag, which runs the *whole process* inside one worktree via `os.chdir` (`vibe/cli/entrypoint.py:311-321`). Nothing connects it to subagents. Real-git tests exist at `tests/cli/test_worktree.py`.

### 1.5 What the prior analysis did not cover — the actual deep blockers

**(a) Process-global working directory — the single biggest blocker.** Every file tool resolves relative paths against `Path.cwd()`: `write_file.py:115`, `edit.py:214`, and `bash` spawns subprocesses without a `cwd` argument, inheriting the process cwd (`bash.py:99-127`). `AgentLoop` and `InvokeContext` (`vibe/core/tools/base.py:51-70`) have **no working-directory concept**. Even with a worktree prepared per worker, there is currently no way to point a child loop's tools at it — `os.chdir` is process-global and would corrupt all siblings and the parent. This must be fixed for any isolation strategy to work in-process.

**(b) Checkpoints / rewind assume a single writer.** Each `AgentLoop` owns one in-memory `Checkpointer` + `FileStore` with no locking (`_loop.py:488-500`; `vibe/core/checkpoints/checkpointer.py:56-61`). Child loops get their own, so the parent's rewind (`vibe/core/rewind/manager.py:75-120`) *cannot restore files written by subagents*. Two unisolated concurrent writers would leave rewind/review inconsistent. Worktree isolation sidesteps this for the parent checkout, but merge-back is a git operation that rewind will never undo — this must be documented behavior.

**(c) Token/cost accounting does not roll up.** Each loop has its own `AgentStats` (`_loop.py:446`; `types.py:49-141`); `TaskResult` returns only `turns_used` (`task.py:45-48`, `171-189`). Child usage is discarded.

**(d) Session logging is already concurrency-safe** (good news): each child gets its own `SessionLogger` writing to `{parent_session_dir}/agents/{agent}_{ts}_{id}/` (`task.py:120-124`; `session_logger.py:65-76`), and appends are serialized per-instance via `_save_lock` (`session_logger.py:42-45`, `306-345`). No changes needed.

**(e) Approval flow under parallel ASK is serialized, accidentally but usefully.** `_should_execute_tool` holds the *shared* `PermissionStore.lock` while awaiting the approval callback (`_loop.py:1911-1945`), and children share the parent's store (`task.py:132`). Parallel children hitting ASK therefore produce one prompt at a time — correct UX — but a pending prompt blocks *all* siblings' permission checks (not their already-running tools). Acceptable; documented, not changed.

**(f) Scratchpad registry is unsynchronized global state** (`vibe/core/scratchpad.py:9-26`) but children don't create scratchpads (`_loop.py:440-442`), so it is not on the critical path.

---

## 2. Prior art (web-researched)

- **Claude Code**: subagents are Markdown + frontmatter (`tools`, `model`, `permissionMode`); parallel Task fan-out returns only a final text summary — child events do not stream to the user (SDK exposes `parent_tool_use_id` for attribution). Built-in Explore/Plan agents are read-only; write-capable parallelism is *not* solved natively: the docs for agent teams state "partition the work so each teammate owns a different set of files". The community pairs it with **one git worktree per agent** (claude-squad, Crystal/Nimbalyst, Conductor, uzi, gwq, Claudio). Documented failure modes worth avoiding: permission-inheritance races under parallel dispatch (issues #51288, #36983), approval fatigue when children can't reach user-approved permissions (#47221, #73633), and a nasty bug where an edit lands silently in a *sibling worktree's* copy of the same path (#59567).
- **Codex CLI**: OS-level sandboxing (Seatbelt/Landlock+bwrap) with `workspace-write` mode — isolation by *path policy*, not git. Docs explicitly warn to "be more careful with parallel write-heavy workflows". A shared `~/.codex` session dir corrupted parallel instances until worked around with per-instance `CODEX_HOME` — Vibe's per-child session dirs already avoid this class of bug.
- **OpenCode**: primary vs subagent modes like Vibe's `AGENT`/`SUBAGENT`; task spawning was sequential, parallel subtasks added later; worktree-per-session is a requested pattern (issue #12896), not shipped.
- **Gemini CLI**: hub-and-spoke subagents with context isolation and consolidated single-response return; parallel dispatch supported; no isolation story for writers.
- **Consensus across worktree orchestrators**: one worktree + one branch per agent; **never auto-resolve merge conflicts** (the agent that didn't write the other side can't recover intent); auto-merge at most when clean; report conflicts for a human/orchestrator. Worktrees solve runtime file conflicts, not semantic conflicts — task partitioning remains the orchestrator's job (that's the teammate's workflow layer).

Patterns stolen: worktree-per-worker on a named branch; clean-merge-only with conflict *reporting*; parent-visible child event streams with parent/child ids; per-child session dirs. Mistakes avoided: approval fatigue (sandbox-scoped auto-approve instead), permission races (shared `PermissionStore` + lock already serializes), shared-session corruption (already per-child), silent cross-worktree writes (path allowlist pinned to the worker's worktree).

---

## 3. Isolation options compared

| Strategy | Safety | Merge-back | Cost in this codebase | Demo legibility |
|---|---|---|---|---|
| **A. Git worktree per worker** | Strong: OS-level separate trees; parent checkout untouched; sibling writes can't collide at runtime | First-class: branch per worker, `git merge-tree` conflict detection, clean merges automatable | Low-medium: `worktree.py` lifecycle already exists + tested; needs per-loop workdir threading (needed by *every* option) and merge helpers | Excellent: branches/diffs are visible, narratable artifacts |
| B. Disjoint file-set assignment w/ enforcement | Medium: prevents declared overlaps only; bash escapes trivially; depends on orchestrator declaring sets correctly | None needed (shared tree) — but partial failures leave a half-mutated tree with no revert unit | Low: `resolve_file_tool_permission` allowlists already support it (`write_file.py:81-89`, PLAN precedent `models.py:90-97`) | Poor: no per-worker diff artifact; failures hard to show |
| C. Copy/overlay sandbox (copy tree or overlayfs) | Strong runtime isolation | Weak: no VCS identity for changes; merge = fragile diff/patch application; overlayfs is Linux-only (Vibe ships on macOS/Windows too — AGENTS.md cross-platform rule) | High: new copy/sync machinery, big-repo cost, nothing to reuse | Medium |
| D. Serialized write phases (parallel plan, serial apply) | Strong (only one writer at a time) | Trivial | Medium: new phase orchestration in the loop | Poor: it is not actually parallel writing — fails the mission |

**Decision: A (worktree per worker), reusing B's allowlist mechanism as the in-sandbox permission guard.** Rationale: it is the industry-consensus pattern, `vibe/core/worktree.py` already implements the hard parts, git gives a free merge-back and audit story, and each worker's branch is a legible demo artifact. Option B's enforcement mechanism (permission `never` + path allowlist, exactly how the PLAN profile confines writes to `PLANS_DIR`) is reused to pin each worker's write tools inside its own worktree — which also structurally prevents the Claude Code cross-worktree silent-write bug.

---

## 4. Chosen design

### 4.1 Worker subagent profile

`vibe/core/agents/models.py`:

```python
class AgentIsolation(StrEnum):
    NONE = auto()
    WORKTREE = auto()

class BuiltinAgentName(StrEnum):
    ...
    WORKER = "worker"

@dataclass(frozen=True)
class AgentProfile:
    ...
    isolation: AgentIsolation = AgentIsolation.NONE   # new field; from_toml pops "isolation"

WORKER = AgentProfile(
    name=BuiltinAgentName.WORKER,
    display_name="Worker",
    description="Write-capable subagent that implements code in an isolated git worktree",
    safety=AgentSafety.DESTRUCTIVE,
    agent_type=AgentType.SUBAGENT,
    isolation=AgentIsolation.WORKTREE,
    overrides={
        "enabled_tools": ["grep", "read_file", "edit", "write_file", "bash", "todo"],
        "system_prompt_id": "worker",
    },
)
```

- New prompt `vibe/core/prompts/worker.md`: implement exactly the assigned task inside the current directory (the worktree); do not `git merge`/`push`/switch branches; report a concise summary of changes as the final message.
- Register in `BUILTIN_AGENTS` (`models.py:191-198`).
- Custom workers: existing TOML discovery (`.vibe/agents/*.toml`, `~/.vibe/agents/*.toml`) with `agent_type = "subagent"`, `isolation = "worktree"`, and any `enabled_tools`. `AgentProfile.from_toml` (`models.py:71-84`) gains the `isolation` pop.
- `prompts/task.md:9` is rewritten: read-only applies to explore-style agents; worker-style agents modify files *only inside their isolated worktree*.
- Spawn permission is unchanged mechanics: `worker` is **not** in the default `TaskToolConfig.allowlist`, so spawning a worker hits ASK and the user approves fan-out per call (or adds `"worker"` to `[tools.task].allowlist` to trust it).

### 4.2 Per-loop working directory (prerequisite for any isolation)

The smallest change that makes worktrees usable in-process:

- `AgentLoop.__init__(..., working_dir: Path | None = None)`; stored as `self.working_dir`.
- `InvokeContext` (`vibe/core/tools/base.py:51-70`) gains `working_dir: Path | None = field(default=None)`; populated from `self.working_dir` where the loop builds the context (`_loop.py:1841-1859`).
- Tools honor it (`base = ctx.working_dir or Path.cwd()` at the existing resolution points):
  - `write_file.py:115`, `edit.py:214` — relative-path anchoring.
  - `read_file` / `grep` — same anchoring for relative paths and default search root.
  - `bash.py` — pass `cwd=str(ctx.working_dir)` (when set) to all three `create_subprocess_*` call sites (`bash.py:99`, `110`, `121`), and use it instead of `Path.cwd()` in the outside-workdir path analysis (`bash.py:335`).
  - `managed_bash` sessions spawn with the same cwd.
- `working_dir=None` (the default everywhere) preserves current behavior byte-for-byte.

### 4.3 Task tool: spawn, sandbox, merge-back

`TaskToolConfig` (`task.py:51-53`) grows:

```python
class TaskToolConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ASK
    allowlist: list[str] = Field(default=[BuiltinAgentName.EXPLORE])
    max_parallel: int = Field(default=4)                     # concurrent subagent loops
    merge: Literal["manual", "auto"] = Field(default="manual")
    keep_worktrees: Literal["always", "on-failure", "never"] = Field(default="on-failure")
```

`Task.run` flow for a profile with `isolation == WORKTREE`:

1. **Guard**: not inside a git repo → `ToolError("worker agents require a git repository")`. A write-capable profile (any of `edit`/`write_file`/`bash` enabled) with `isolation == NONE` → `ToolError` (deliberate constraint: unisolated concurrent writers corrupt checkpoint/rewind semantics, §1.5b).
2. **Concurrency cap**: acquire a per-parent-loop `asyncio.Semaphore(config.max_parallel)` (owned by the parent loop, exposed via `InvokeContext`, so the cap spans one session rather than one process).
3. **Prepare worktree** off the event loop (git is blocking; ADR 0003 responsiveness): `wt = await asyncio.to_thread(prepare_worktree_session, f"vibe-{args.agent}-{ctx.tool_call_id[:8]}", Path.cwd())`.
4. **Spawn child** as today (`task.py:126-139`) plus `working_dir=wt.path` and runtime tool-config overrides pinning the sandbox (see §4.5).
5. **Stamp and forward** child events (see §5); accumulate the final response as today.
6. **Commit**: `await asyncio.to_thread(commit_worktree, wt, message=f"vibe worker: {args.task[:72]}")` — `git add -A && git commit` inside the worktree; no-op if clean.
7. **Merge-back** per `config.merge`:
   - `manual` (default): no merge. `TaskResult` reports the branch; the orchestrator/user merges.
   - `auto`: run `merge_report` (`git merge-tree --write-tree <head> <branch>` in the invoking checkout); **only if conflict-free**, `git merge --no-ff <branch>`; otherwise leave the branch and report `merge_status="conflicts"` with the conflicting paths. Conflicts are **always reported, never auto-resolved**.
8. **Cleanup** per `keep_worktrees` (`remove_worktree(wt, delete_branch=False)` — the branch survives until merged; `never` still keeps worktrees whose branch is unmerged).

New helpers in `vibe/core/worktree.py` (same module, same `WorktreeError` hierarchy, GitPython like the rest of the file):

```python
def commit_worktree(worktree: PreparedWorktree, message: str) -> str | None: ...   # commit sha or None if clean

@dataclass(frozen=True)
class MergeReport:
    clean: bool
    conflicting_paths: tuple[str, ...]
    files_changed: tuple[str, ...]

def merge_report(repo_root: Path, branch: str, base_commit: str) -> MergeReport: ...
def merge_branch(repo_root: Path, branch: str) -> str: ...                        # merge commit sha; raises WorktreeError on conflict
```

`TaskResult` (`task.py:45-48`) grows (all optional, backward compatible):

```python
class TaskResult(BaseModel):
    response: str
    turns_used: int
    completed: bool
    agent_id: str | None = None            # child loop session_id
    branch: str | None = None
    worktree_path: str | None = None
    commit: str | None = None
    files_changed: list[str] = Field(default_factory=list)
    merge_status: Literal["not_attempted", "merged", "conflicts", "no_changes"] = "not_attempted"
    conflicting_paths: list[str] = Field(default_factory=list)
    prompt_tokens: int = 0                 # rolled up from child AgentStats (§1.5c)
    completion_tokens: int = 0
```

### 4.4 Fixing token rollup

After the child stream is drained, copy `subagent_loop.stats.session_prompt_tokens` / `session_completion_tokens` into `TaskResult` and `SubagentFinishedEvent` (§5). The parent's own `AgentStats` is *not* mutated (its `session_cost` is priced per-loop model; the workflow layer aggregates from events).

### 4.5 Approval semantics for parallel writers

Principle: **auto-approve file writes inside the worker's own worktree; hard-deny outside; everything else keeps the normal permission flow.** Justification: this is not a bypass of ADR 0004 — it uses the exact mechanism the builtin PLAN profile uses to confine writes to `PLANS_DIR` (`models.py:90-97` → `resolve_file_tool_permission`, `write_file.py:81-89`), and the same trust rationale as the scratchpad ("You can read and write files here without permission prompts", `task.py:142-147`): the blast radius is a throwaway branch in a throwaway worktree, reviewable before merge.

At spawn, the task tool injects runtime overrides into the child's config (deep-merged like agent-profile overrides):

```python
{"tools": {
    "write_file": {"permission": "never", "allowlist": [str(wt.root / "**")]},
    "edit":       {"permission": "never", "allowlist": [str(wt.root / "**")]},
}}
```

- Relative paths resolve inside the worktree (§4.2) → allowlisted → `ALWAYS`. Absolute paths outside → `NEVER` (denied, not asked — deliberate: parallel background workers must not generate approval-fatigue prompts for out-of-sandbox writes; this is the documented Claude Code failure mode).
- `bash` keeps its normal config: safe-command allowlist auto-runs, everything else ASKs through the propagated parent callback (`task.py:138-139`), serialized one-prompt-at-a-time by the shared `PermissionStore.lock` (§1.5e). Users who want fully autonomous workers set `[tools.bash] permission = "always"` in their custom worker TOML — an explicit, user-owned escalation.
- Approval prompts raised by children must be attributable: the UI reads the agent identity from the in-flight `SubagentStartedEvent` mapping (`tool_call_id` → agent), no callback signature change required.

### 4.6 Failure and cancellation semantics

- **Sibling isolation** (already true, now guaranteed by tests): each `task` call is an independent `asyncio` task (`_loop.py:1659-1662`); a child exception is caught inside `Task.run` and produces `TaskResult(completed=False)` with the error text (`task.py:175-180`) — siblings and the parent turn continue.
- **Ctrl-C / parent cancellation**: existing path (§1.1) cancels all children; each child loop is closed via the `finally` (`task.py:181-183`). Worktrees are kept under `keep_worktrees="on-failure"` (cancel counts as failure) so partial work is inspectable; the branch always survives. A `SubagentFinishedEvent(status="cancelled")` is emitted best-effort; consumers must treat a missing finished-event after cancellation as terminal (a closing generator cannot always deliver it).
- **Checkpoints/rewind**: worker writes never touch the parent checkout, so parent checkpoint/rewind is unaffected. An `auto` merge is a git operation **outside** rewind's file-snapshot model — rewinding past a merged turn does not un-merge. Documented in the worker/task prompts and README. (Flagged: this is a scoped, documented gap, not an ADR violation — ADR 0006 rewind semantics for messages are untouched.)

### 4.7 Config surface (per ADR 0005)

All keys flow through the existing layered `tools` dict (`vibe/core/config/vibe_schema.py:160-164`, deep-merged) and agent-profile TOML — no new top-level sections:

```toml
# ~/.vibe/config.toml or .vibe/config.toml
[tools.task]
allowlist = ["explore", "worker"]   # opt-in: spawn workers without a prompt
max_parallel = 4
merge = "manual"                    # "manual" | "auto" (clean merges only)
keep_worktrees = "on-failure"       # "always" | "on-failure" | "never"
```

```toml
# .vibe/agents/rust-worker.toml — custom worker profile
description = "Implements Rust changes"
agent_type = "subagent"
isolation = "worktree"
safety = "destructive"
enabled_tools = ["grep", "read_file", "edit", "write_file", "bash"]
```

Defaults preserve today's behavior exactly: `worker` exists but is not allowlisted, `merge="manual"`, explore-only flows are untouched.

---

## 5. Event contract (public API for the workflow layer & visualization)

### 5.1 Identity model — backward-compatible change to `BaseEvent`

`vibe/core/types.py`:

```python
class AgentIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)
    agent_id: str                 # child loop session_id (already unique, already in SessionMetadata)
    parent_id: str | None = None  # parent loop session_id
    name: str                     # profile name, e.g. "worker", "explore"

class BaseEvent(BaseModel, ABC):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    agent: AgentIdentity | None = None   # None ⇒ the root agent (backward compatible)
```

`agent is None` means "root agent" — every existing producer and consumer keeps working unchanged; existing tests that construct events without the field remain valid. Nesting deeper than one level (future) composes naturally via `parent_id` chains.

### 5.2 New lifecycle events

```python
class SubagentStartedEvent(BaseEvent):
    tool_call_id: str                     # the parent task tool call
    task: str
    isolation: Literal["none", "worktree"]
    branch: str | None = None
    worktree_path: Path | None = None

class SubagentFinishedEvent(BaseEvent):
    tool_call_id: str
    status: Literal["completed", "failed", "cancelled"]
    turns_used: int
    prompt_tokens: int
    completion_tokens: int
    branch: str | None = None
    merge_status: Literal["not_attempted", "merged", "conflicts", "no_changes"] = "not_attempted"
```

Both carry `agent=AgentIdentity(...)` for the *child*. Emitted by the task tool around the child run.

### 5.3 Forwarding child events

The task tool stops swallowing the child stream (`task.py:152-169`). Instead it re-yields, verbatim but stamped, this whitelist of child events: `AssistantEvent`, `ReasoningEvent`, `ToolCallEvent`, `ToolResultEvent`, `ToolStreamEvent` — each with `agent=AgentIdentity(agent_id=child.session_id, parent_id=ctx.session_id, name=args.agent)` (`model_copy(update=...)`, events are cheap Pydantic models). The response accumulation for `TaskResult.response` is unchanged.

Loop plumbing (one surgical change): `_invoke_tool` currently treats any non-`ToolStreamEvent` yield as the result (`_loop.py:1862-1865`). It becomes:

```python
if isinstance(item, BaseEvent):
    yield item          # ToolStreamEvent and forwarded child events alike
else:
    result_model = item # tool results are BaseModel, never BaseEvent
```

with `BaseTool.run`'s generator type widened from `AsyncGenerator[ToolStreamEvent | ResultT, None]` to `AsyncGenerator[BaseEvent | ResultT, None]` (`base.py`), and the corresponding unions in `_handle_tool_calls` / `_run_tools_concurrently` / `_execute_tool_call` widened to include `BaseEvent` subclasses. Parent-loop bookkeeping is untouched: it keys off the tool's final *result model*, never off forwarded events.

### 5.4 Ordering & attribution guarantees (contract)

1. Events from one agent (`agent.agent_id`) are delivered in that agent's causal order.
2. Events from different agents may interleave arbitrarily (queue multiplexing, `_loop.py:1673-1677`).
3. Every forwarded child event has non-`None` `agent`; every root event has `agent is None`.
4. `SubagentStartedEvent` precedes all forwarded events of that agent; `SubagentFinishedEvent` follows them (best-effort under cancellation, §4.6).
5. A child event's parentage to a specific fan-out call is `SubagentStartedEvent.tool_call_id` + `agent.agent_id` (join key for the visualization).

### 5.5 Surface compatibility

- **Textual UI** (`event_handler.py:145-190`): first checks `event.agent`; child events render as indented/dim activity lines under the owning task-tool widget (found via the `tool_call_id`→agent map from `SubagentStartedEvent`). Root behavior unchanged. Rich per-agent panels are the visualization project's job, not this spec's.
- **ACP bridge** (`acp_agent_loop.py:1708-1796`): filters `event.agent is not None` except `SubagentStarted/Finished` mapped to the existing tool-call progress updates — i.e. ACP clients keep today's summarized view until the protocol grows subagent support. **ADR 0003 flag**: this introduces events one surface consumes richly and another intentionally down-renders; per the ADR this is flagged here explicitly. Ignoring stamped events is always safe, which keeps the contract meaningful for every surface.
- **Session transcripts** (ADR 0006): unchanged — transcripts store `LLMMessage`s, not events; child transcripts already live in their own directories (§1.5d). No migration.

---

## 6. Acceptance criteria (mechanically checkable)

- [ ] `uv run pytest` passes with all new tests below.
- [ ] `uv run pyright` (strict) passes with zero new errors.
- [ ] `uv run ruff check .` and `uv run ruff format --check .` pass.
- [ ] `BUILTIN_AGENTS["worker"]` exists with `agent_type == SUBAGENT`, `isolation == WORKTREE`, and write tools enabled.
- [ ] Two `task(agent="worker")` calls with per-child latency `t` complete in `< 1.75 * t` wall-clock (proves ~`max(t1,t2)`, not `t1+t2`).
- [ ] Each concurrent worker gets a distinct worktree and branch; the parent checkout's `git status` is empty after both finish (merge="manual").
- [ ] Every forwarded child event carries `agent.agent_id == <child session_id>` and `agent.parent_id == <parent session_id>`; all root events have `agent is None`.
- [ ] `SubagentStartedEvent`/`SubagentFinishedEvent` bracket each child's forwarded events and carry the branch name and token counts.
- [ ] A worker's `write_file`/`edit` outside its worktree is denied without any approval prompt; inside, it executes without any prompt.
- [ ] With `merge="auto"`: non-conflicting branches merge into the invoking checkout; conflicting branches are left unmerged with `merge_status="conflicts"` and the conflicting paths listed. No merge-conflict markers ever reach the working tree.
- [ ] One worker raising an exception does not cancel its sibling; the sibling's `TaskResult.completed is True`.
- [ ] Cancelling the parent turn closes both child loops (`aclose` awaited) and leaves no orphan asyncio tasks.
- [ ] Existing test suites (`tests/tools/test_task.py`, `tests/agent_loop/`, `tests/cli/test_worktree.py`) pass unmodified except where event/result models gained optional fields.
- [ ] `vibe/core/skills/builtins/vibe.py` documents the worker agent and the new `[tools.task]` keys.

## 7. Test plan

Uses existing infrastructure only: `pytest` + `pytest-asyncio` (10s timeout, xdist), autouse `config_dir`/`tmp_working_directory` isolation (`tests/conftest.py:135-164`), `FakeBackend` (`tests/stubs/fake_backend.py`) for scripting parent turns, and the `patch("vibe.core.tools.builtins.task.AgentLoop")` child-mocking pattern already used in `tests/tools/test_task.py:171-176`. Real-git tests follow `tests/cli/test_worktree.py`. Layout per AGENTS.md: `vibe/core/worktree.py` sits directly in `vibe/core/` → tests flat in `tests/core/`; tool tests in `tests/tools/`; loop tests in `tests/agent_loop/`.

| File | Proves |
|---|---|
| `tests/tools/test_task_parallel.py` | **Real concurrency**: parent `AgentLoop` + `FakeBackend` emits one assistant turn with two `task` calls; each mocked child `act()` sleeps 0.3s. Assert wall-clock `< 0.55s` via `time.monotonic()` (~max, not sum). **Interleaving/attribution**: collect the event stream; assert both agents' forwarded events carry distinct `agent.agent_id`s, correct `parent_id`, and that events from the two agents interleave (both ids appear before either's `SubagentFinishedEvent`). Semaphore test: `max_parallel=1` forces `≈ 0.6s` serial execution. |
| `tests/tools/test_task_failure_isolation.py` | Child A raises mid-`act()`, child B completes: A's `TaskResult.completed is False` with error text, B's is `True`; parent turn finishes; B's events all delivered. |
| `tests/tools/test_task_cancellation.py` | Start two mocked children (long sleeps), `aclose()` the parent's `act()` generator mid-flight: both children's `aclose` awaited (pattern of `test_task.py:243-261`), no lingering tasks in `asyncio.all_tasks()`, worktree dirs still on disk under `keep_worktrees="on-failure"`. |
| `tests/tools/test_task_worktree.py` | Integration with a real git repo in `tmp_working_directory` (init + commit, as in `tests/cli/test_worktree.py`): spawning `agent="worker"` creates a worktree + branch named `vibe-worker-<id>`; mocked child writes a file into its `working_dir` kwarg; after the run the branch contains the commit, the parent checkout is untouched; `keep_worktrees` policies honored; non-git cwd → `ToolError`; write-capable profile with `isolation="none"` → `ToolError`. |
| `tests/tools/test_task_events.py` | `SubagentStartedEvent` precedes forwarded events, `SubagentFinishedEvent` follows with `turns_used` and token counts; forwarded whitelist exact (no child `UserMessageEvent` leaks); root events have `agent is None`. |
| `tests/tools/test_task_sandbox_permissions.py` | Child config overrides pin `write_file`/`edit` to the worktree: path inside → `ALWAYS` (no approval callback invoked), absolute path outside → denied (`skipped=True`), approval callback never called for either. Extends patterns from `tests/tools/test_scratchpad_permissions.py` and `tests/tools/test_granular_permissions.py`. |
| `tests/agent_loop/test_agent_event_forwarding.py` | Loop-level: a `FakeTool` yielding an arbitrary stamped `BaseEvent` has it forwarded verbatim by `_invoke_tool`; the non-event yield is still taken as the result; `ToolStreamEvent` behavior unchanged. |
| `tests/core/test_worktree_merge.py` | `commit_worktree` (dirty → sha, clean → `None`); `merge_report` clean vs conflicting (two branches touching the same line); `merge_branch` merges clean and raises `WorktreeError` on conflict leaving the checkout pristine (no `MERGE_HEAD`, no conflict markers). Real git repos, no mocks. |
| `tests/agent_loop/test_agents.py` (extend) | Worker profile shape: subagent type, worktree isolation, enabled tools; `from_toml` parses `isolation = "worktree"`; unknown isolation value → validation error. |
| `tests/tools/test_bash.py` / `tests/tools/test_invoke_context.py` (extend) | `InvokeContext.working_dir` anchors bash subprocess cwd and relative file-tool paths; `None` preserves `Path.cwd()` behavior (assert explicitly, not host-dependently, per AGENTS.md cross-platform rule). |

Timing tests stay well inside the global 10s `pytest-timeout`; sleeps are 0.2–0.4s with generous assertions margins for xdist-loaded machines.

## 8. Ordered implementation task list

1. **Event identity foundation** — `AgentIdentity`, `BaseEvent.agent`, `SubagentStartedEvent`, `SubagentFinishedEvent` in `vibe/core/types.py`. No behavior change. (Tests: model round-trips.)
2. **Loop event pass-through** — widen `BaseTool.run` generator type (`vibe/core/tools/base.py`) and the `isinstance(item, BaseEvent)` forwarding in `_invoke_tool` (`_loop.py:1862-1865`) plus the affected generator annotations. (Test: `tests/agent_loop/test_agent_event_forwarding.py`.)
3. **Per-loop working dir** — `AgentLoop.working_dir`, `InvokeContext.working_dir`, honor it in `write_file`, `edit`, `read_file`, `grep`, `bash` (incl. subprocess `cwd`), `managed_bash`. (Tests: extend `test_bash.py`, `test_invoke_context.py`.)
4. **Worktree helpers** — `commit_worktree`, `MergeReport`, `merge_report`, `merge_branch` in `vibe/core/worktree.py`. (Test: `tests/core/test_worktree_merge.py`.)
5. **Worker profile** — `AgentIsolation`, `AgentProfile.isolation`, `BuiltinAgentName.WORKER`, `WORKER` profile, `from_toml` support, `vibe/core/prompts/worker.md`, update `prompts/task.md`. (Test: extend `test_agents.py`.)
6. **Task tool: isolation + sandbox** — `TaskToolConfig` fields, worktree preparation via `asyncio.to_thread`, `working_dir` wiring, runtime write-tool allowlist overrides, guards (non-git, unisolated writers), semaphore. (Tests: `test_task_worktree.py`, `test_task_sandbox_permissions.py`.)
7. **Task tool: event forwarding + rollup** — stamp/forward child events, emit started/finished events, extend `TaskResult` with branch/commit/usage fields. (Tests: `test_task_events.py`, `test_task_parallel.py`, `test_task_failure_isolation.py`, `test_task_cancellation.py`.)
8. **Merge-back** — `merge="manual"|"auto"` in `Task.run` epilogue + `keep_worktrees` cleanup. (Test: extend `test_task_worktree.py` for auto-merge clean/conflict.)
9. **Surface adaptation** — Textual `event_handler.py` renders `agent`-stamped events under the owning task widget; ACP bridge filters child events to preserve current client behavior. (Snapshot tests via `pytest-textual-snapshot` where practical.)
10. **Docs & skill** — update `vibe/core/skills/builtins/vibe.py` (per AGENTS.md), README section on workers, and note the rewind-vs-merge limitation.

Steps 1–4 are independent of each other and individually shippable no-ops; 5–8 build the feature; 9–10 polish. Every step keeps `uv run pytest`, `uv run pyright`, `uv run ruff check .` green.

## 9. Explicit flags (per ADR guidance)

- **ADR 0003**: forwarded child events are richly consumed by the Textual UI/programmatic surface but intentionally down-rendered by ACP until the protocol supports subagents — flagged as required ("a new event is useful for one surface…"). Mitigation: stamped events are always safe to ignore.
- **ADR 0006 / rewind**: rewinding past an auto-merged worker turn does not revert the git merge (file snapshots don't model git operations). Documented behavior, default `merge="manual"` keeps the parent checkout untouched entirely.
- **Deliberate constraint**: write-capable subagents without worktree isolation are rejected rather than supported, because the single-writer `Checkpointer` (`_loop.py:488-500`) makes unisolated concurrent writers unsound. Lifting this would be a separate, larger project.
