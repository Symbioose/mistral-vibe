from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
import inspect
import json
import os
import time
import traceback
from typing import Any, Protocol

from vibe.core.logger import logger
from vibe.core.meowmeowmeow.errors import MeowMeowMeowError, MeowMeowMeowScriptError
from vibe.core.meowmeowmeow.events import (
    AgentFinishedEvent,
    AgentProgressEvent,
    AgentStartedEvent,
    MeowMeowMeowEvent,
    MeowMeowMeowFinishedEvent,
    MeowMeowMeowLogEvent,
    PhaseStartedEvent,
)
from vibe.core.meowmeowmeow.journal import MeowMeowMeowJournal, journal_key
from vibe.core.meowmeowmeow.models import (
    AgentRunStatus,
    MeowMeowMeowRunOutcome,
    MeowMeowMeowStatus,
    SubagentOutcome,
    SubagentRequest,
)
from vibe.core.meowmeowmeow.script import (
    MEOWMEOWMEOW_MAIN_NAME,
    ParsedScript,
    build_script_globals,
)
from vibe.core.meowmeowmeow.structured import (
    check_schema_valid,
    parse_structured,
    schema_prompt_suffix,
)

DEFAULT_MAX_AGENTS = 1000
MAX_FANOUT_ITEMS = 4096
DEFAULT_SCHEMA_RETRIES = 2
_LABEL_MAX_LEN = 60
_LOG_MAX_LEN = 2000
_PROMPT_EVENT_MAX_LEN = 6000


class SubagentSpawner(Protocol):
    async def run(
        self, request: SubagentRequest, on_progress: Callable[[str], None]
    ) -> SubagentOutcome: ...


def default_max_concurrency() -> int:
    return max(1, min(16, (os.cpu_count() or 4) - 2))


class MeowMeowMeowRuntime:
    def __init__(
        self,
        script: ParsedScript,
        spawner: SubagentSpawner,
        *,
        args: Any = None,
        prompts: dict[str, str] | None = None,
        fast_model: str | None = None,
        on_event: Callable[[MeowMeowMeowEvent], None] | None = None,
        journal: MeowMeowMeowJournal | None = None,
        max_concurrency: int | None = None,
        max_agents: int = DEFAULT_MAX_AGENTS,
        schema_retries: int = DEFAULT_SCHEMA_RETRIES,
    ) -> None:
        self._script = script
        self._spawner = spawner
        self._args = args
        self._prompts = dict(prompts) if prompts else {}
        self._fast_model = fast_model
        self._on_event = on_event
        self._journal = journal
        self._semaphore = asyncio.Semaphore(
            max_concurrency
            if max_concurrency and max_concurrency > 0
            else default_max_concurrency()
        )
        self._max_agents = max_agents
        self._schema_retries = schema_retries
        self._agent_total = 0
        self._agents_cached = 0
        self._current_phase: str | None = None
        self._seen_phases: set[str] = set()
        self._result_value: Any = None
        self._result_set = False
        self._tasks: set[asyncio.Task[Any]] = set()

    async def run(self) -> MeowMeowMeowRunOutcome:
        start = time.monotonic()
        ns = build_script_globals(self._primitives())
        exec(self._script.code, ns)
        main = ns[MEOWMEOWMEOW_MAIN_NAME]
        status = MeowMeowMeowStatus.COMPLETED
        value: Any = None
        error: str | None = None
        try:
            returned = await main()
            value = returned if returned is not None else self._result_value
        except asyncio.CancelledError:
            status = MeowMeowMeowStatus.CANCELLED
            self._cancel_pending_tasks()
            raise
        except MeowMeowMeowError as e:
            status = MeowMeowMeowStatus.FAILED
            error = str(e)
        except Exception as e:
            status = MeowMeowMeowStatus.FAILED
            error = self._format_script_error(e)
        finally:
            self._cancel_pending_tasks()
            if status is not MeowMeowMeowStatus.CANCELLED:
                self._emit(
                    MeowMeowMeowFinishedEvent(
                        status=status,
                        agents_spawned=self._agent_total,
                        agents_cached=self._agents_cached,
                        duration_s=time.monotonic() - start,
                        error=error,
                    )
                )
        return MeowMeowMeowRunOutcome(
            status=status,
            value=value,
            error=error,
            agents_spawned=self._agent_total,
            agents_cached=self._agents_cached,
            duration_s=time.monotonic() - start,
        )

    def _primitives(self) -> dict[str, Any]:
        return {
            "agent": self._agent,
            "parallel": self._parallel,
            "pipeline": self._pipeline,
            "phase": self._phase,
            "log": self._log,
            "result": self._result,
            "args": self._args,
            "prompts": self._prompts,
            "fast_model": self._fast_model,
        }

    async def _agent(
        self,
        prompt: str,
        *,
        label: str | None = None,
        phase: str | None = None,
        schema: dict[str, Any] | None = None,
        agent_name: str | None = None,
        model: str | None = None,
    ) -> Any:
        if not isinstance(prompt, str) or not prompt.strip():
            raise MeowMeowMeowScriptError("agent() prompt must be a non-empty string")
        if schema is not None:
            self._check_schema(schema)
        self._agent_total += 1
        if self._agent_total > self._max_agents:
            raise MeowMeowMeowError(
                f"meow_meow_meow exceeded the lifetime cap of {self._max_agents} agent() calls"
            )
        agent_id = self._agent_total
        display = label or self._truncate_label(prompt)
        phase_title = phase if phase is not None else self._current_phase
        key = journal_key(prompt, schema, agent_name, model)

        if self._journal is not None:
            hit, cached = self._journal.consume(key)
            if hit:
                self._agents_cached += 1
                self._journal.record(key, display, cached)
                self._emit(
                    AgentStartedEvent(
                        agent_id=agent_id,
                        label=display,
                        phase=phase_title,
                        cached=True,
                        prompt=self._truncate_prompt(prompt),
                    )
                )
                self._emit(
                    AgentFinishedEvent(
                        agent_id=agent_id,
                        label=display,
                        status=AgentRunStatus.OK,
                        cached=True,
                        output=self._preview(cached),
                    )
                )
                return cached

        async with self._semaphore:
            self._emit(
                AgentStartedEvent(
                    agent_id=agent_id,
                    label=display,
                    phase=phase_title,
                    prompt=self._truncate_prompt(prompt),
                )
            )
            started = time.monotonic()
            try:
                result, run_status, detail = await self._run_agent(
                    prompt, schema, agent_name, model, agent_id
                )
            except asyncio.CancelledError:
                self._emit(
                    AgentFinishedEvent(
                        agent_id=agent_id,
                        label=display,
                        status=AgentRunStatus.CANCELLED,
                        duration_s=time.monotonic() - started,
                    )
                )
                raise
        self._emit(
            AgentFinishedEvent(
                agent_id=agent_id,
                label=display,
                status=run_status,
                duration_s=time.monotonic() - started,
                detail=detail,
                output=self._preview(result),
            )
        )
        if run_status is AgentRunStatus.OK and self._journal is not None:
            self._journal.record(key, display, result)
        return result

    async def _run_agent(
        self,
        prompt: str,
        schema: dict[str, Any] | None,
        agent_name: str | None,
        model: str | None,
        agent_id: int,
    ) -> tuple[Any, AgentRunStatus, str | None]:
        def on_progress(message: str) -> None:
            self._emit(AgentProgressEvent(agent_id=agent_id, message=message))

        current_prompt = (
            prompt if schema is None else prompt + schema_prompt_suffix(schema)
        )
        last_error: str | None = None
        for _attempt in range(self._schema_retries + 1):
            request = SubagentRequest(
                prompt=current_prompt, agent_name=agent_name, model=model
            )
            try:
                outcome = await self._spawner.run(request, on_progress)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("MeowMeowMeow subagent crashed: %s", e)
                return None, AgentRunStatus.ERROR, str(e)
            if not outcome.success:
                return None, AgentRunStatus.ERROR, outcome.error or "subagent failed"
            if schema is None:
                return outcome.text, AgentRunStatus.OK, None
            parsed, err = parse_structured(outcome.text, schema)
            if err is None:
                return parsed, AgentRunStatus.OK, None
            last_error = err
            current_prompt = (
                prompt
                + schema_prompt_suffix(schema)
                + f"\n\nYour previous attempt was rejected: {err}\n"
                "Respond with ONLY the corrected JSON value."
            )
        return (
            None,
            AgentRunStatus.ERROR,
            f"structured output failed validation after "
            f"{self._schema_retries + 1} attempts: {last_error}",
        )

    async def _parallel(self, thunks: Iterable[Callable[[], Any]]) -> list[Any]:
        thunk_list = list(thunks)
        self._check_fanout(len(thunk_list), "parallel")
        tasks = [self._spawn_task(self._call_thunk(t)) for t in thunk_list]
        return await self._gather(tasks)

    async def _pipeline(
        self, items: Iterable[Any], *stages: Callable[..., Any]
    ) -> list[Any]:
        item_list = list(items)
        self._check_fanout(len(item_list), "pipeline")
        if not stages:
            raise MeowMeowMeowScriptError("pipeline() requires at least one stage")
        tasks = [
            self._spawn_task(self._run_chain(item, index, stages))
            for index, item in enumerate(item_list)
        ]
        return await self._gather(tasks)

    async def _gather(self, tasks: list[asyncio.Task[Any]]) -> list[Any]:
        try:
            return await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            raise

    async def _call_thunk(self, thunk: Callable[[], Any]) -> Any:
        try:
            value = thunk()
            if inspect.isawaitable(value):
                value = await value
            return value
        except asyncio.CancelledError:
            raise
        except MeowMeowMeowError:
            raise
        except Exception as e:
            logger.debug("MeowMeowMeow parallel thunk failed: %s", e)
            return None

    async def _run_chain(
        self, item: Any, index: int, stages: tuple[Callable[..., Any], ...]
    ) -> Any:
        prev = item
        for stage in stages:
            try:
                prev = await self._call_stage(stage, prev, item, index)
            except asyncio.CancelledError:
                raise
            except MeowMeowMeowError:
                raise
            except Exception as e:
                logger.debug(
                    "MeowMeowMeow pipeline stage failed for item %d: %s", index, e
                )
                return None
        return prev

    async def _call_stage(
        self, stage: Callable[..., Any], prev: Any, item: Any, index: int
    ) -> Any:
        arity = self._stage_arity(stage)
        call_args = (prev, item, index)[:arity]
        value = stage(*call_args)
        if inspect.isawaitable(value):
            value = await value
        return value

    @staticmethod
    def _stage_arity(stage: Callable[..., Any]) -> int:
        try:
            sig = inspect.signature(stage)
        except (TypeError, ValueError):
            return 1
        positional = [
            p
            for p in sig.parameters.values()
            if p.kind in {p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD}
        ]
        if any(p.kind == p.VAR_POSITIONAL for p in sig.parameters.values()):
            return 3
        return max(1, min(3, len(positional)))

    def _phase(self, title: str) -> None:
        if not isinstance(title, str) or not title.strip():
            raise MeowMeowMeowScriptError("phase() title must be a non-empty string")
        self._current_phase = title
        if title not in self._seen_phases:
            self._seen_phases.add(title)
            self._emit(PhaseStartedEvent(title=title))

    def _log(self, message: Any) -> None:
        text = str(message)
        if len(text) > _LOG_MAX_LEN:
            text = text[: _LOG_MAX_LEN - 1] + "…"
        self._emit(MeowMeowMeowLogEvent(message=text))

    def _result(self, value: Any) -> None:
        try:
            json.dumps(value)
        except (TypeError, ValueError) as e:
            raise MeowMeowMeowScriptError(
                f"result() value must be JSON-serializable: {e}"
            ) from e
        self._result_value = value
        self._result_set = True

    def _spawn_task(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _cancel_pending_tasks(self) -> None:
        for task in list(self._tasks):
            if not task.done():
                task.cancel()

    def _emit(self, event: MeowMeowMeowEvent) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception as e:
            logger.warning("MeowMeowMeow event callback failed: %s", e)

    @staticmethod
    def _check_schema(schema: Any) -> None:
        if not isinstance(schema, dict):
            raise MeowMeowMeowScriptError(
                "agent() schema must be a JSON Schema dict, e.g. "
                '{"type": "object", "properties": {"summary": {"type": "string"}}}'
            )
        try:
            json.dumps(schema)
        except (TypeError, ValueError) as e:
            raise MeowMeowMeowScriptError(
                "agent() schema must be JSON-serializable — use "
                '{"type": "string"} style, never Python types like str'
            ) from e
        schema_error = check_schema_valid(schema)
        if schema_error is not None:
            raise MeowMeowMeowScriptError(
                f"agent() schema is not a valid JSON Schema: {schema_error}"
            )

    @staticmethod
    def _check_fanout(count: int, name: str) -> None:
        if count > MAX_FANOUT_ITEMS:
            raise MeowMeowMeowScriptError(
                f"{name}() accepts at most {MAX_FANOUT_ITEMS} items, got {count}"
            )

    @staticmethod
    def _preview(value: Any) -> str | None:
        if value is None:
            return None
        text = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        )
        if len(text) > _PROMPT_EVENT_MAX_LEN:
            return text[: _PROMPT_EVENT_MAX_LEN - 1] + "…"
        return text

    @staticmethod
    def _truncate_prompt(prompt: str) -> str:
        if len(prompt) > _PROMPT_EVENT_MAX_LEN:
            return prompt[: _PROMPT_EVENT_MAX_LEN - 1] + "…"
        return prompt

    @staticmethod
    def _truncate_label(prompt: str) -> str:
        first_line = prompt.strip().splitlines()[0]
        if len(first_line) > _LABEL_MAX_LEN:
            return first_line[: _LABEL_MAX_LEN - 1] + "…"
        return first_line

    def _format_script_error(self, e: Exception) -> str:
        frames = traceback.extract_tb(e.__traceback__)
        script_file = f"<meow_meow_meow:{self._script.meta.name}>"
        script_frames = [f for f in frames if f.filename == script_file]
        location = ""
        if script_frames:
            last = script_frames[-1]
            location = f" (script line {last.lineno})"
        message = f"{type(e).__name__}: {e}{location}"
        if script_frames:
            source_lines = self._script.source.splitlines()
            lineno = script_frames[-1].lineno
            if lineno is not None and 1 <= lineno <= len(source_lines):
                message += f"\n  offending line: {source_lines[lineno - 1].strip()!r}"
        if isinstance(e, KeyError) and e.args:
            key = e.args[0]
            if isinstance(key, str) and ('"' in key or "'" in key):
                message += (
                    f"\n  The missing key {key!r} contains literal quote "
                    'characters — you wrote x[\'"key"\'] instead of x["key"].'
                )
        if isinstance(e, (KeyError, TypeError, AttributeError, IndexError)):
            message += (
                "\nThis usually means the script indexed into an agent result "
                "without guarantees about its shape. Pass schema= to agent() so "
                "the result is validated JSON, and guard for None (failed agents "
                "return None). Fix the script and re-invoke with "
                "resume_from_run_id — successful agents replay from the journal."
            )
        return message
