from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import inspect
from pathlib import Path
from typing import Any

import pytest

from vibe.core.mioumioumiou.events import (
    AgentFinishedEvent,
    AgentStartedEvent,
    MiouMiouMiouLogEvent,
    PhaseStartedEvent,
)
from vibe.core.mioumioumiou.journal import MiouMiouMiouJournal
from vibe.core.mioumioumiou.models import (
    AgentRunStatus,
    MiouMiouMiouStatus,
    SubagentOutcome,
    SubagentRequest,
)
from vibe.core.mioumioumiou.runtime import MiouMiouMiouRuntime
from vibe.core.mioumioumiou.script import parse_miou_miou_miou_script

Responder = Callable[[SubagentRequest], SubagentOutcome | Awaitable[SubagentOutcome]]


class FakeSpawner:
    def __init__(self, responder: Responder | None = None) -> None:
        self.calls: list[SubagentRequest] = []
        self.active = 0
        self.max_active = 0
        self._responder = responder or (
            lambda _req: SubagentOutcome(success=True, text="ok")
        )

    async def run(
        self, request: SubagentRequest, on_progress: Callable[[str], None]
    ) -> SubagentOutcome:
        self.calls.append(request)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            outcome = self._responder(request)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            return outcome
        finally:
            self.active -= 1


def make_script(body: str, name: str = "test") -> str:
    return f'meta = {{"name": "{name}", "description": "test miou_miou_miou"}}\n{body}'


async def run_miou_miou_miou(
    body: str,
    spawner: FakeSpawner | None = None,
    *,
    args: Any = None,
    journal: MiouMiouMiouJournal | None = None,
    max_concurrency: int | None = None,
    max_agents: int = 1000,
    schema_retries: int = 2,
) -> tuple[Any, FakeSpawner, list[Any]]:
    spawner = spawner or FakeSpawner()
    events: list[Any] = []
    runtime = MiouMiouMiouRuntime(
        parse_miou_miou_miou_script(make_script(body)),
        spawner,
        args=args,
        on_event=events.append,
        journal=journal,
        max_concurrency=max_concurrency,
        max_agents=max_agents,
        schema_retries=schema_retries,
    )
    outcome = await runtime.run()
    return outcome, spawner, events


@pytest.mark.asyncio
async def test_simple_agent_call() -> None:
    outcome, spawner, _events = await run_miou_miou_miou(
        'response = await agent("do something")\nresult(response)'
    )
    assert outcome.status is MiouMiouMiouStatus.COMPLETED
    assert outcome.value == "ok"
    assert outcome.agents_spawned == 1
    assert spawner.calls[0].prompt == "do something"


@pytest.mark.asyncio
async def test_top_level_return() -> None:
    outcome, _spawner, _events = await run_miou_miou_miou(
        'value = await agent("x")\nreturn {"got": value}'
    )
    assert outcome.status is MiouMiouMiouStatus.COMPLETED
    assert outcome.value == {"got": "ok"}


@pytest.mark.asyncio
async def test_args_passthrough() -> None:
    outcome, _spawner, _events = await run_miou_miou_miou(
        "result(args['items'])", args={"items": [1, 2]}
    )
    assert outcome.value == [1, 2]


@pytest.mark.asyncio
async def test_prompts_passthrough() -> None:
    spawner = FakeSpawner()
    events: list[Any] = []
    runtime = MiouMiouMiouRuntime(
        parse_miou_miou_miou_script(
            make_script('value = await agent(prompts["deep"])\nreturn value')
        ),
        spawner,
        prompts={"deep": "a very long brief"},
        on_event=events.append,
    )
    outcome = await runtime.run()
    assert outcome.status is MiouMiouMiouStatus.COMPLETED
    assert spawner.calls[0].prompt == "a very long brief"


@pytest.mark.asyncio
async def test_parallel_runs_thunks_and_maps_errors_to_none() -> None:
    def responder(req: SubagentRequest) -> SubagentOutcome:
        if "bad" in req.prompt:
            return SubagentOutcome(success=False, error="boom")
        return SubagentOutcome(success=True, text=req.prompt)

    body = (
        "outs = await parallel([\n"
        '    lambda: agent("one"),\n'
        '    lambda: agent("bad"),\n'
        "    lambda: (_ for _ in ()).throw(ValueError('thunk crashed')),\n"
        '    lambda: agent("two"),\n'
        "])\n"
        "result(outs)"
    )
    outcome, _spawner, _events = await run_miou_miou_miou(body, FakeSpawner(responder))
    assert outcome.status is MiouMiouMiouStatus.COMPLETED
    assert outcome.value == ["one", None, None, "two"]


@pytest.mark.asyncio
async def test_pipeline_stages_and_arity() -> None:
    body = (
        "async def stage_two(prev, item, index):\n"
        '    return f"{prev}|{item}|{index}"\n'
        "outs = await pipeline(\n"
        '    ["a", "b"],\n'
        "    lambda item: agent(item),\n"
        "    stage_two,\n"
        ")\n"
        "result(outs)"
    )
    responder = lambda req: SubagentOutcome(success=True, text=req.prompt.upper())
    outcome, _spawner, _events = await run_miou_miou_miou(body, FakeSpawner(responder))
    assert outcome.status is MiouMiouMiouStatus.COMPLETED
    assert outcome.value == ["A|a|0", "B|b|1"]


@pytest.mark.asyncio
async def test_pipeline_stage_error_drops_item_to_none() -> None:
    body = (
        "def explode(prev):\n"
        "    raise KeyError('nope')\n"
        "outs = await pipeline([1, 2], lambda item: item * 10, explode)\n"
        "result(outs)"
    )
    outcome, _spawner, _events = await run_miou_miou_miou(body)
    assert outcome.status is MiouMiouMiouStatus.COMPLETED
    assert outcome.value == [None, None]


@pytest.mark.asyncio
async def test_pipeline_has_no_barrier_between_stages() -> None:
    gate = asyncio.Event()

    async def responder(req: SubagentRequest) -> SubagentOutcome:
        if req.prompt == "s1-slow":
            await gate.wait()
        if req.prompt == "s2-fast":
            gate.set()
        return SubagentOutcome(success=True, text=req.prompt)

    body = (
        "outs = await pipeline(\n"
        '    ["slow", "fast"],\n'
        '    lambda item: agent(f"s1-{item}"),\n'
        '    lambda prev, item, i: agent(f"s2-{item}"),\n'
        ")\n"
        "result(outs)"
    )
    outcome, _spawner, events = await run_miou_miou_miou(
        body, FakeSpawner(responder), max_concurrency=4
    )
    assert outcome.status is MiouMiouMiouStatus.COMPLETED
    assert outcome.value == ["s2-slow", "s2-fast"]
    finished = [e.label for e in events if isinstance(e, AgentFinishedEvent)]
    assert finished.index("s2-fast") < finished.index("s1-slow")


@pytest.mark.asyncio
async def test_concurrency_cap_respected() -> None:
    async def responder(_req: SubagentRequest) -> SubagentOutcome:
        await asyncio.sleep(0.01)
        return SubagentOutcome(success=True, text="ok")

    spawner = FakeSpawner(responder)
    body = (
        "outs = await parallel([\n"
        + "\n".join(f'    lambda: agent("p{i}"),' for i in range(8))
        + "\n])\nresult(len([o for o in outs if o]))"
    )
    outcome, spawner, _events = await run_miou_miou_miou(
        body, spawner, max_concurrency=2
    )
    assert outcome.status is MiouMiouMiouStatus.COMPLETED
    assert outcome.value == 8
    assert spawner.max_active <= 2


@pytest.mark.asyncio
async def test_schema_validation_with_retry() -> None:
    attempts: list[str] = []

    def responder(req: SubagentRequest) -> SubagentOutcome:
        attempts.append(req.prompt)
        if len(attempts) == 1:
            return SubagentOutcome(success=True, text="not json")
        return SubagentOutcome(success=True, text='{"n": 5}')

    body = (
        'value = await agent("count", schema={"type": "object", '
        '"properties": {"n": {"type": "integer"}}, "required": ["n"]})\n'
        "result(value)"
    )
    outcome, _spawner, _events = await run_miou_miou_miou(body, FakeSpawner(responder))
    assert outcome.status is MiouMiouMiouStatus.COMPLETED
    assert outcome.value == {"n": 5}
    assert len(attempts) == 2
    assert "rejected" in attempts[1]


@pytest.mark.asyncio
async def test_schema_exhausted_retries_returns_none() -> None:
    responder = lambda _req: SubagentOutcome(success=True, text="never json")
    body = (
        'value = await agent("x", schema={"type": "object"})\n'
        "result({'value': value})"
    )
    outcome, spawner, events = await run_miou_miou_miou(
        body, FakeSpawner(responder), schema_retries=1
    )
    assert outcome.status is MiouMiouMiouStatus.COMPLETED
    assert outcome.value == {"value": None}
    assert len(spawner.calls) == 2
    finished = [e for e in events if isinstance(e, AgentFinishedEvent)]
    assert finished[0].status is AgentRunStatus.ERROR


@pytest.mark.asyncio
async def test_failed_agent_returns_none() -> None:
    responder = lambda _req: SubagentOutcome(success=False, error="dead")
    outcome, _spawner, events = await run_miou_miou_miou(
        'value = await agent("x")\nresult({"value": value})', FakeSpawner(responder)
    )
    assert outcome.status is MiouMiouMiouStatus.COMPLETED
    assert outcome.value == {"value": None}
    finished = [e for e in events if isinstance(e, AgentFinishedEvent)]
    assert finished[0].status is AgentRunStatus.ERROR
    assert finished[0].detail == "dead"


@pytest.mark.asyncio
async def test_phases_and_logs_emit_events() -> None:
    body = (
        'phase("Scan")\n'
        'log("starting scan")\n'
        'await agent("a", phase="Custom")\n'
        'phase("Fix")\n'
        'await agent("b")\n'
        "result(None)"
    )
    _outcome, _spawner, events = await run_miou_miou_miou(body)
    phase_titles = [e.title for e in events if isinstance(e, PhaseStartedEvent)]
    assert phase_titles == ["Scan", "Fix"]
    logs = [e.message for e in events if isinstance(e, MiouMiouMiouLogEvent)]
    assert logs == ["starting scan"]
    started = [e for e in events if isinstance(e, AgentStartedEvent)]
    assert started[0].phase == "Custom"
    assert started[1].phase == "Fix"


@pytest.mark.asyncio
async def test_print_is_logged() -> None:
    _outcome, _spawner, events = await run_miou_miou_miou(
        'print("hello", 1)\nresult(None)'
    )
    logs = [e.message for e in events if isinstance(e, MiouMiouMiouLogEvent)]
    assert logs == ["hello 1"]


@pytest.mark.asyncio
async def test_max_agents_cap_fails_miou_miou_miou() -> None:
    body = "for i in range(5):\n    await agent(f'p{i}')\nresult(None)"
    outcome, spawner, _events = await run_miou_miou_miou(body, max_agents=3)
    assert outcome.status is MiouMiouMiouStatus.FAILED
    assert outcome.error is not None
    assert "lifetime cap" in outcome.error
    assert len(spawner.calls) == 3


@pytest.mark.asyncio
async def test_fanout_cap_fails_miou_miou_miou() -> None:
    body = "await parallel([lambda: agent('x')] * 5000)\nresult(None)"
    outcome, _spawner, _events = await run_miou_miou_miou(body)
    assert outcome.status is MiouMiouMiouStatus.FAILED
    assert outcome.error is not None
    assert "at most" in outcome.error


@pytest.mark.asyncio
async def test_script_exception_fails_with_location() -> None:
    outcome, _spawner, _events = await run_miou_miou_miou("x = {}\nx['missing']")
    assert outcome.status is MiouMiouMiouStatus.FAILED
    assert outcome.error is not None
    assert "KeyError" in outcome.error
    assert "script line" in outcome.error
    assert "schema=" in outcome.error
    assert "resume_from_run_id" in outcome.error


@pytest.mark.asyncio
async def test_banned_time_fails_miou_miou_miou() -> None:
    outcome, _spawner, _events = await run_miou_miou_miou("t = time.time()\nresult(t)")
    assert outcome.status is MiouMiouMiouStatus.FAILED
    assert outcome.error is not None
    assert "unavailable" in outcome.error


@pytest.mark.asyncio
async def test_non_serializable_result_rejected() -> None:
    outcome, _spawner, _events = await run_miou_miou_miou("result(lambda: 1)")
    assert outcome.status is MiouMiouMiouStatus.FAILED
    assert outcome.error is not None
    assert "JSON-serializable" in outcome.error


@pytest.mark.asyncio
async def test_journal_replay_skips_spawner(tmp_path: Path) -> None:
    journal_one = MiouMiouMiouJournal.create(tmp_path / "run1.jsonl")
    body = 'value = await agent("expensive")\nresult(value)'
    outcome_one, spawner_one, _e = await run_miou_miou_miou(body, journal=journal_one)
    assert outcome_one.status is MiouMiouMiouStatus.COMPLETED
    assert len(spawner_one.calls) == 1

    journal_two = MiouMiouMiouJournal.create(
        tmp_path / "run2.jsonl", resume_from=tmp_path / "run1.jsonl"
    )
    outcome_two, spawner_two, events_two = await run_miou_miou_miou(
        body, journal=journal_two
    )
    assert outcome_two.status is MiouMiouMiouStatus.COMPLETED
    assert outcome_two.value == "ok"
    assert len(spawner_two.calls) == 0
    assert outcome_two.agents_cached == 1
    started = [e for e in events_two if isinstance(e, AgentStartedEvent)]
    assert started[0].cached


@pytest.mark.asyncio
async def test_journal_replay_only_matches_same_prompt(tmp_path: Path) -> None:
    journal_one = MiouMiouMiouJournal.create(tmp_path / "run1.jsonl")
    await run_miou_miou_miou(
        'await agent("old prompt")\nresult(None)', journal=journal_one
    )

    journal_two = MiouMiouMiouJournal.create(
        tmp_path / "run2.jsonl", resume_from=tmp_path / "run1.jsonl"
    )
    _outcome, spawner_two, _events = await run_miou_miou_miou(
        'await agent("new prompt")\nresult(None)', journal=journal_two
    )
    assert len(spawner_two.calls) == 1


@pytest.mark.asyncio
async def test_failed_agents_not_journaled(tmp_path: Path) -> None:
    responder_fail = lambda _req: SubagentOutcome(success=False, error="boom")
    journal_one = MiouMiouMiouJournal.create(tmp_path / "run1.jsonl")
    await run_miou_miou_miou(
        'await agent("flaky")\nresult(None)',
        FakeSpawner(responder_fail),
        journal=journal_one,
    )

    journal_two = MiouMiouMiouJournal.create(
        tmp_path / "run2.jsonl", resume_from=tmp_path / "run1.jsonl"
    )
    _outcome, spawner_two, _events = await run_miou_miou_miou(
        'await agent("flaky")\nresult(None)', journal=journal_two
    )
    assert len(spawner_two.calls) == 1


@pytest.mark.asyncio
async def test_cancellation_propagates() -> None:
    started = asyncio.Event()

    async def responder(_req: SubagentRequest) -> SubagentOutcome:
        started.set()
        await asyncio.sleep(30)
        return SubagentOutcome(success=True, text="never")

    runtime = MiouMiouMiouRuntime(
        parse_miou_miou_miou_script(make_script('await agent("x")\nresult(None)')),
        FakeSpawner(responder),
    )
    task = asyncio.create_task(runtime.run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_progress_callback_emits_events() -> None:
    async def responder(_req: SubagentRequest) -> SubagentOutcome:
        return SubagentOutcome(success=True, text="ok")

    class ProgressSpawner(FakeSpawner):
        async def run(
            self, request: SubagentRequest, on_progress: Callable[[str], None]
        ) -> SubagentOutcome:
            on_progress("read_file: done")
            return await super().run(request, on_progress)

    _outcome, _spawner, events = await run_miou_miou_miou(
        'await agent("x")\nresult(None)', ProgressSpawner(responder)
    )
    progress = [e for e in events if e.kind == "agent_progress"]
    assert progress and progress[0].message == "read_file: done"


@pytest.mark.asyncio
async def test_empty_script_body_completes() -> None:
    outcome, _spawner, _events = await run_miou_miou_miou("")
    assert outcome.status is MiouMiouMiouStatus.COMPLETED
    assert outcome.value is None
