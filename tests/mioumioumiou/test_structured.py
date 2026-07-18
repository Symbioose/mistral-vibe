from __future__ import annotations

from vibe.core.mioumioumiou.structured import parse_structured

SCHEMA = {
    "type": "object",
    "properties": {"bugs": {"type": "array", "items": {"type": "string"}}},
    "required": ["bugs"],
}


def test_plain_json() -> None:
    value, err = parse_structured('{"bugs": ["a", "b"]}', SCHEMA)
    assert err is None
    assert value == {"bugs": ["a", "b"]}


def test_fenced_json() -> None:
    text = 'Here you go:\n```json\n{"bugs": []}\n```\nDone.'
    value, err = parse_structured(text, SCHEMA)
    assert err is None
    assert value == {"bugs": []}


def test_json_with_surrounding_prose() -> None:
    text = 'The result is {"bugs": ["x"]} as requested.'
    value, err = parse_structured(text, SCHEMA)
    assert err is None
    assert value == {"bugs": ["x"]}


def test_invalid_json_reports_error() -> None:
    value, err = parse_structured("not json at all", SCHEMA)
    assert value is None
    assert err is not None


def test_schema_mismatch_reports_error() -> None:
    value, err = parse_structured('{"wrong": true}', SCHEMA)
    assert value is None
    assert err is not None
    assert "schema validation failed" in err


def test_empty_response() -> None:
    value, err = parse_structured("   ", SCHEMA)
    assert value is None
    assert err == "empty response"


def test_array_schema() -> None:
    value, err = parse_structured("[1, 2, 3]", {"type": "array"})
    assert err is None
    assert value == [1, 2, 3]
