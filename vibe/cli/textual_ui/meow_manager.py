from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from functools import lru_cache
from pathlib import Path

from vibe.core.audio_player import AudioFormat, AudioPlayerPort
from vibe.core.logger import logger

MEOW_WAV_PATH = Path(__file__).parent / "assets" / "meow.wav"
MEOW_DELAY_SECONDS = 0.3


@lru_cache(maxsize=1)
def load_meow_wav() -> bytes:
    return MEOW_WAV_PATH.read_bytes()


class MeowManager:
    """Plays a single meow when the agent finishes its turn; a keystroke cuts it off.

    Playback errors permanently disable meowing for the session instead of
    surfacing to the UI.
    """

    def __init__(
        self,
        *,
        is_enabled: Callable[[], bool],
        should_meow: Callable[[], bool],
        player: AudioPlayerPort | None = None,
        delay: float = MEOW_DELAY_SECONDS,
    ) -> None:
        self._is_enabled = is_enabled
        self._should_meow = should_meow
        self._player = player
        self._delay = delay
        self._task: asyncio.Task[None] | None = None
        self._unavailable = False

    @property
    def is_meowing(self) -> bool:
        return self._task is not None and not self._task.done()

    def meow_once(self) -> None:
        if self._unavailable or self.is_meowing or not self._is_enabled():
            return
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
        if self._player is not None:
            with suppress(Exception):
                self._player.stop()

    async def _run(self) -> None:
        # Give any queued follow-up turn a beat to start before meowing.
        await asyncio.sleep(self._delay)
        if self._is_enabled() and self._should_meow():
            self._play_once()

    def _play_once(self) -> None:
        try:
            player = self._get_player()
            if not player.is_playing:
                player.play(load_meow_wav(), AudioFormat.WAV)
        except Exception as exc:
            self._unavailable = True
            logger.info("Meow disabled: audio playback unavailable", exc_info=exc)

    def _get_player(self) -> AudioPlayerPort:
        if self._player is None:
            from vibe.core.audio_player.audio_player import AudioPlayer

            self._player = AudioPlayer()
        return self._player
