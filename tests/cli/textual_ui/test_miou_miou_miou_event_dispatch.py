from __future__ import annotations

import pytest
from textual.widget import Widget

from vibe.cli.textual_ui.handlers.event_handler import EventHandler
from vibe.cli.textual_ui.widgets.miou_miou_miou import MiouMiouMiouCallMessage
from vibe.cli.textual_ui.widgets.tools import ToolCallMessage
from vibe.core.tools.builtins.bash import Bash
from vibe.core.tools.builtins.miou_miou_miou import MiouMiouMiou
from vibe.core.types import ToolCallEvent, ToolStreamEvent


def make_handler(mounted: list[Widget]) -> EventHandler:
    async def mount_callback(widget: Widget, after: Widget | None = None) -> None:
        mounted.append(widget)

    return EventHandler(
        mount_callback=mount_callback, get_tools_collapsed=lambda: False
    )


@pytest.mark.asyncio
async def test_miou_miou_miou_tool_call_gets_miou_miou_miou_widget() -> None:
    mounted: list[Widget] = []
    handler = make_handler(mounted)
    event = ToolCallEvent(
        tool_call_id="tc1", tool_name="miou_miou_miou", tool_class=MiouMiouMiou
    )
    widget = await handler.handle_event(event)
    assert isinstance(widget, MiouMiouMiouCallMessage)
    assert mounted == [widget]


@pytest.mark.asyncio
async def test_other_tool_call_gets_generic_widget() -> None:
    mounted: list[Widget] = []
    handler = make_handler(mounted)
    event = ToolCallEvent(tool_call_id="tc1", tool_name="bash", tool_class=Bash)
    widget = await handler.handle_event(event)
    assert type(widget) is ToolCallMessage


@pytest.mark.asyncio
async def test_stream_event_with_data_routes_to_miou_miou_miou_widget() -> None:
    mounted: list[Widget] = []
    handler = make_handler(mounted)
    call_event = ToolCallEvent(
        tool_call_id="tc1", tool_name="miou_miou_miou", tool_class=MiouMiouMiou
    )
    widget = await handler.handle_event(call_event)
    assert isinstance(widget, MiouMiouMiouCallMessage)

    seen: list[dict] = []

    async def record(data: dict) -> None:
        seen.append(data)

    widget.handle_miou_miou_miou_event = record  # type: ignore[method-assign]
    stream = ToolStreamEvent(
        tool_call_id="tc1",
        tool_name="miou_miou_miou",
        message="Phase: Scan",
        data={"kind": "phase_started", "title": "Scan"},
    )
    await handler.handle_event(stream)
    assert seen == [{"kind": "phase_started", "title": "Scan"}]
