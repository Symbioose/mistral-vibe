from __future__ import annotations

from pathlib import Path

from vibe.core.mioumioumiou.journal import MiouMiouMiouJournal, journal_key


def test_journal_key_stable_and_distinct() -> None:
    a = journal_key("p", None, None, None)
    assert a == journal_key("p", None, None, None)
    assert a != journal_key("q", None, None, None)
    assert a != journal_key("p", {"type": "object"}, None, None)
    assert a != journal_key("p", None, "explore", None)


def test_record_and_replay(tmp_path: Path) -> None:
    first = MiouMiouMiouJournal.create(tmp_path / "journal.jsonl")
    key = journal_key("prompt", None, None, None)
    first.record(key, "label", "result-1")
    first.record(key, "label", "result-2")

    resumed = MiouMiouMiouJournal.create(
        tmp_path / "journal2.jsonl", resume_from=tmp_path / "journal.jsonl"
    )
    hit, value = resumed.consume(key)
    assert hit and value == "result-1"
    hit, value = resumed.consume(key)
    assert hit and value == "result-2"
    hit, value = resumed.consume(key)
    assert not hit


def test_consume_unknown_key(tmp_path: Path) -> None:
    journal = MiouMiouMiouJournal.create(tmp_path / "journal.jsonl")
    hit, value = journal.consume("nope")
    assert not hit
    assert value is None


def test_corrupt_lines_skipped(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    key = journal_key("p", None, None, None)
    good = MiouMiouMiouJournal.create(path)
    good.record(key, "l", {"ok": True})
    with path.open("a", encoding="utf-8") as f:
        f.write("{not json\n")
    resumed = MiouMiouMiouJournal.create(tmp_path / "new.jsonl", resume_from=path)
    hit, value = resumed.consume(key)
    assert hit and value == {"ok": True}


def test_resume_from_missing_file(tmp_path: Path) -> None:
    journal = MiouMiouMiouJournal.create(
        tmp_path / "j.jsonl", resume_from=tmp_path / "missing.jsonl"
    )
    assert not journal.consume("anything")[0]
