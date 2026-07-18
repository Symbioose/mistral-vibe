from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from tests.conftest import build_test_vibe_config
from vibe.core.agents.manager import AgentManager
from vibe.core.config.orchestrator_legacy import LegacyConfigOrchestrator
from vibe.core.telemetry.types import LaunchContext, TerminalEmulator
from vibe.core.tools.base import InvokeContext
from vibe.core.types import AgentStats, BaseEvent, LLMMessage, Role

type ActFn = Callable[[str], AsyncGenerator[BaseEvent, None]]


def make_task_ctx(
    tool_call_id: str = "test-call-id", session_id: str | None = "parent-session"
) -> InvokeContext:
    config = build_test_vibe_config()
    manager = AgentManager(LegacyConfigOrchestrator(config))
    return InvokeContext(
        tool_call_id=tool_call_id,
        agent_manager=manager,
        session_id=session_id,
        launch_context=LaunchContext(
            agent_entrypoint="cli",
            agent_version="1.0.0",
            client_name="vibe_cli",
            client_version="1.0.0",
            terminal_emulator=TerminalEmulator.VSCODE,
        ),
    )


def make_fake_subagent_loop(
    act: ActFn,
    messages: list[LLMMessage] | None = None,
    session_id: str = "child-session",
    stats: AgentStats | None = None,
) -> MagicMock:
    loop = MagicMock()
    loop.act = act
    loop.messages = (
        messages
        if messages is not None
        else [LLMMessage(role=Role.assistant, content="done")]
    )
    loop.session_id = session_id
    loop.stats = stats or AgentStats()
    loop.set_approval_callback = MagicMock()
    loop.aclose = AsyncMock()
    return loop


def make_fake_subagent_loop_factory(
    act_factory: Callable[[dict[str, Any]], ActFn],
) -> tuple[Callable[..., MagicMock], list[MagicMock]]:
    """Build an AgentLoop replacement whose act() sees the constructor kwargs.

    Returns the factory (for ``patch(..., side_effect=factory)``) and the list
    of loops it created.
    """
    created: list[MagicMock] = []

    def factory(*_args: Any, **kwargs: Any) -> MagicMock:
        loop = make_fake_subagent_loop(
            act_factory(kwargs), session_id=f"child-{len(created)}"
        )
        created.append(loop)
        return loop

    return factory, created
