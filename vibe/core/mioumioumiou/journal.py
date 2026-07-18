from __future__ import annotations

from collections import deque
import hashlib
import json
from pathlib import Path
from typing import Any

from vibe.core.logger import logger


def journal_key(
    prompt: str,
    schema: dict[str, Any] | None,
    agent_name: str | None,
    model: str | None,
) -> str:
    payload = json.dumps(
        {"prompt": prompt, "schema": schema, "agent_name": agent_name, "model": model},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class MiouMiouMiouJournal:
    def __init__(self, path: Path, replay: dict[str, deque[Any]] | None = None) -> None:
        self._path = path
        self._replay = replay or {}
        path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def create(cls, path: Path, resume_from: Path | None = None) -> MiouMiouMiouJournal:
        replay: dict[str, deque[Any]] = {}
        if resume_from is not None and resume_from.exists():
            for line in resume_from.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        "Skipping corrupt miou_miou_miou journal line in %s",
                        resume_from,
                    )
                    continue
                replay.setdefault(entry["key"], deque()).append(entry["result"])
        return cls(path, replay)

    def consume(self, key: str) -> tuple[bool, Any]:
        bucket = self._replay.get(key)
        if not bucket:
            return False, None
        result = bucket.popleft()
        if not bucket:
            del self._replay[key]
        return True, result

    def record(self, key: str, label: str, result: Any) -> None:
        entry = json.dumps(
            {"key": key, "label": label, "result": result}, ensure_ascii=False
        )
        with self._path.open("a", encoding="utf-8") as f:
            f.write(entry + "\n")
