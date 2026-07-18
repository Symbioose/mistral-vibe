from __future__ import annotations

import json
import re
from typing import Any

import jsonschema

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def check_schema_valid(schema: dict[str, Any]) -> str | None:
    validator = jsonschema.validators.validator_for(schema)
    try:
        validator.check_schema(schema)
    except jsonschema.SchemaError as e:
        return e.message
    return None


def schema_prompt_suffix(schema: dict[str, Any]) -> str:
    return (
        "\n\nReturn ONLY a JSON value matching this JSON Schema "
        "(no prose, no code fences):\n"
        f"{json.dumps(schema, ensure_ascii=False)}"
    )


def parse_structured(text: str, schema: dict[str, Any]) -> tuple[Any, str | None]:
    candidate, err = _extract_json(text)
    if err is not None:
        return None, err
    try:
        jsonschema.validate(candidate, schema)
    except jsonschema.ValidationError as e:
        return None, f"schema validation failed: {e.message}"
    return candidate, None


def _extract_json(text: str) -> tuple[Any, str | None]:
    text = text.strip()
    if not text:
        return None, "empty response"
    attempts = [text]
    fence = _FENCE_RE.search(text)
    if fence:
        attempts.insert(0, fence.group(1).strip())
    for start_char, end_char in (("{", "}"), ("[", "]")):
        start = text.find(start_char)
        end = text.rfind(end_char)
        if start != -1 and end > start:
            attempts.append(text[start : end + 1])
    last_error = "no JSON value found"
    for attempt in attempts:
        try:
            return json.loads(attempt), None
        except json.JSONDecodeError as e:
            last_error = f"invalid JSON: {e}"
    return None, last_error
