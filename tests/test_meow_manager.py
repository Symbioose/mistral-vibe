from __future__ import annotations

import asyncio
from collections.abc import Callable
import io
import time
import wave

import pytest

from tests.conftest import build_test_agent_loop, build_test_vibe_app
from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.cli.textual_ui.meow_manager import MeowManager, load_meow_wav
from vibe.core.audio_player import AudioFormat


class FakePlayer:
    def __init__(self) -> None:
        self.play_calls: list[bytes] = []
        self.stop_calls: int = 0

    @property
    def is_playing(self) -> bool:
        return False

    def play(
        self,
        audio_data: bytes,
        audio_format: AudioFormat,
        *,
        on_finished: Callable[[], object] | None = None,
    ) -> None:
        self.play_calls.append(audio_data)

    def stop(self) -> None:
        self.stop_calls += 1


def test_bundled_meow_is_valid_wav() -> None:
    data = load_meow_wav()
    with wave.open(io.BytesIO(data), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        duration = wav_file.getnframes() / wav_file.getframerate()
        assert 0.5 < duration < 5.0
        frames = wav_file.readframes(wav_file.getnframes())
    assert any(byte != 0 for byte in frames)


@pytest.mark.asyncio
async def test_meows_exactly_once() -> None:
    player = FakePlayer()
    manager = MeowManager(
        is_enabled=lambda: True,
        should_meow=lambda: True,
        player=player,
        delay=0.0,
    )
    manager.meow_once()
    manager.meow_once()
    await asyncio.sleep(0.05)
    assert len(player.play_calls) == 1
    await asyncio.sleep(0.05)
    assert len(player.play_calls) == 1


@pytest.mark.asyncio
async def test_stop_cancels_pending_meow_and_playback() -> None:
    player = FakePlayer()
    manager = MeowManager(
        is_enabled=lambda: True,
        should_meow=lambda: True,
        player=player,
        delay=0.05,
    )
    manager.meow_once()
    manager.stop()
    await asyncio.sleep(0.1)
    assert player.play_calls == []
    assert player.stop_calls == 1
    assert not manager.is_meowing


@pytest.mark.asyncio
async def test_does_not_meow_when_disabled_or_busy() -> None:
    player = FakePlayer()
    disabled_manager = MeowManager(
        is_enabled=lambda: False,
        should_meow=lambda: True,
        player=player,
        delay=0.0,
    )
    disabled_manager.meow_once()
    assert not disabled_manager.is_meowing

    busy_manager = MeowManager(
        is_enabled=lambda: True,
        should_meow=lambda: False,
        player=player,
        delay=0.0,
    )
    busy_manager.meow_once()
    await asyncio.sleep(0.05)
    assert player.play_calls == []


@pytest.mark.asyncio
async def test_playback_error_disables_meowing_silently() -> None:
    class BrokenPlayer(FakePlayer):
        def play(
            self,
            audio_data: bytes,
            audio_format: AudioFormat,
            *,
            on_finished: Callable[[], object] | None = None,
        ) -> None:
            raise RuntimeError("no audio device")

    manager = MeowManager(
        is_enabled=lambda: True,
        should_meow=lambda: True,
        player=BrokenPlayer(),
        delay=0.0,
    )
    manager.meow_once()
    await asyncio.sleep(0.05)
    assert not manager.is_meowing

    manager.meow_once()
    assert not manager.is_meowing


@pytest.mark.asyncio
async def test_app_meows_once_after_turn_and_keystroke_cuts_it() -> None:
    backend = FakeBackend([mock_llm_chunk(content="Response")])
    agent_loop = build_test_agent_loop(backend=backend)
    app = build_test_vibe_app(agent_loop=agent_loop)
    async with app.run_test() as pilot:
        player = FakePlayer()
        app._meow_manager._player = player

        await pilot.press(*"hello")
        assert player.play_calls == []

        await pilot.press("enter")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not player.play_calls:
            await pilot.pause(0.05)
        assert len(player.play_calls) == 1

        await pilot.pause(0.5)
        assert len(player.play_calls) == 1

        await pilot.press("a")
        await pilot.pause(0.05)
        assert player.stop_calls >= 1
