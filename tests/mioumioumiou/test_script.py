from __future__ import annotations

import pytest

from vibe.core.mioumioumiou.errors import MiouMiouMiouScriptError
from vibe.core.mioumioumiou.script import (
    build_script_globals,
    parse_miou_miou_miou_script,
)

VALID_SCRIPT = """
meta = {
    "name": "demo",
    "description": "A demo miou_miou_miou",
    "phases": [{"title": "Scan"}, {"title": "Fix", "detail": "one agent per item"}],
}
result(42)
"""


def test_parse_valid_script() -> None:
    parsed = parse_miou_miou_miou_script(VALID_SCRIPT)
    assert parsed.meta.name == "demo"
    assert parsed.meta.description == "A demo miou_miou_miou"
    assert [p.title for p in parsed.meta.phases] == ["Scan", "Fix"]


def test_missing_meta_rejected() -> None:
    with pytest.raises(MiouMiouMiouScriptError, match="must start with"):
        parse_miou_miou_miou_script("x = 1")


def test_non_literal_meta_rejected() -> None:
    script = 'name = "x"\nmeta = {"name": name, "description": "d"}'
    with pytest.raises(MiouMiouMiouScriptError, match="must start with"):
        parse_miou_miou_miou_script(script)


def test_computed_meta_rejected() -> None:
    script = 'meta = {"name": "a" + "b", "description": "d"}'
    with pytest.raises(MiouMiouMiouScriptError, match="pure literal"):
        parse_miou_miou_miou_script(script)


def test_invalid_meta_name_rejected() -> None:
    script = 'meta = {"name": "Bad Name!", "description": "d"}'
    with pytest.raises(MiouMiouMiouScriptError, match="invalid meta"):
        parse_miou_miou_miou_script(script)


def test_unknown_meta_key_rejected() -> None:
    script = 'meta = {"name": "ok", "description": "d", "phase": []}'
    with pytest.raises(MiouMiouMiouScriptError, match="invalid meta"):
        parse_miou_miou_miou_script(script)


def test_syntax_error_rejected() -> None:
    with pytest.raises(MiouMiouMiouScriptError, match="syntax error"):
        parse_miou_miou_miou_script('meta = {"name": "x", "description": "d"}\ndef (')


def test_imports_rejected() -> None:
    script = 'meta = {"name": "x", "description": "d"}\nimport os'
    with pytest.raises(MiouMiouMiouScriptError, match="imports are unavailable"):
        parse_miou_miou_miou_script(script)


def test_from_imports_rejected() -> None:
    script = 'meta = {"name": "x", "description": "d"}\nfrom os import path'
    with pytest.raises(MiouMiouMiouScriptError, match="imports are unavailable"):
        parse_miou_miou_miou_script(script)


def test_dunder_access_rejected() -> None:
    script = 'meta = {"name": "x", "description": "d"}\ny = ().__class__'
    with pytest.raises(MiouMiouMiouScriptError, match="dunder"):
        parse_miou_miou_miou_script(script)


def test_top_level_await_and_return_compile() -> None:
    script = (
        'meta = {"name": "x", "description": "d"}\n'
        "async def helper():\n    return 1\n"
        "value = await helper()\n"
        "return value\n"
    )
    parsed = parse_miou_miou_miou_script(script)
    assert parsed.meta.name == "x"


def test_shadowing_primitive_variable_rejected() -> None:
    script = (
        'meta = {"name": "x", "description": "d"}\n'
        "for result in [1, 2]:\n    log(result)\n"
    )
    with pytest.raises(
        MiouMiouMiouScriptError, match="'result' is a miou_miou_miou primitive"
    ):
        parse_miou_miou_miou_script(script)


def test_shadowing_primitive_parameter_rejected() -> None:
    script = (
        'meta = {"name": "x", "description": "d"}\n'
        "def helper(agent):\n    return agent\n"
    )
    with pytest.raises(
        MiouMiouMiouScriptError, match="'agent' is a miou_miou_miou primitive"
    ):
        parse_miou_miou_miou_script(script)


def test_shadowing_primitive_function_name_rejected() -> None:
    script = (
        'meta = {"name": "x", "description": "d"}\nasync def phase():\n    return 1\n'
    )
    with pytest.raises(
        MiouMiouMiouScriptError, match="'phase' is a miou_miou_miou primitive"
    ):
        parse_miou_miou_miou_script(script)


def test_shadowing_error_includes_line_number() -> None:
    script = 'meta = {"name": "x", "description": "d"}\nx = 1\nresult = "oops"\n'
    with pytest.raises(MiouMiouMiouScriptError, match="script line 3"):
        parse_miou_miou_miou_script(script)


def test_missing_await_on_agent_call_rejected() -> None:
    script = 'meta = {"name": "x", "description": "d"}\nvalue = agent("prompt")\n'
    with pytest.raises(MiouMiouMiouScriptError, match="write 'await agent"):
        parse_miou_miou_miou_script(script)


def test_missing_await_on_parallel_expr_rejected() -> None:
    script = (
        'meta = {"name": "x", "description": "d"}\nparallel([lambda: agent("a")])\n'
    )
    with pytest.raises(MiouMiouMiouScriptError, match="write 'await parallel"):
        parse_miou_miou_miou_script(script)


def test_missing_await_in_async_def_rejected() -> None:
    script = (
        'meta = {"name": "x", "description": "d"}\n'
        "async def stage(prev):\n"
        "    return pipeline([prev], lambda x: x)\n"
    )
    with pytest.raises(MiouMiouMiouScriptError, match="write 'await pipeline"):
        parse_miou_miou_miou_script(script)


def test_thunk_patterns_are_not_flagged() -> None:
    script = (
        'meta = {"name": "x", "description": "d"}\n'
        'thunks = [lambda: agent("a"), lambda: agent("b")]\n'
        "def make_thunk(prompt_text):\n"
        "    return agent(prompt_text)\n"
        "outs = await parallel(thunks)\n"
        "return outs\n"
    )
    parsed = parse_miou_miou_miou_script(script)
    assert parsed.meta.name == "x"


def test_prompts_is_reserved() -> None:
    script = 'meta = {"name": "x", "description": "d"}\nprompts = {}\n'
    with pytest.raises(
        MiouMiouMiouScriptError, match="'prompts' is a miou_miou_miou primitive"
    ):
        parse_miou_miou_miou_script(script)


def test_syntax_error_shows_offending_line_and_tip() -> None:
    script = 'meta = {"name": "x", "description": "d"}\nx = "unterminated\n'
    with pytest.raises(MiouMiouMiouScriptError) as exc_info:
        parse_miou_miou_miou_script(script)
    message = str(exc_info.value)
    assert "offending line" in message
    assert "unterminated" in message
    assert "prompts" in message


def test_long_string_literal_rejected() -> None:
    prose = "word " * 100
    script = f'meta = {{"name": "x", "description": "d"}}\nbrief = "{prose}"\n'
    with pytest.raises(MiouMiouMiouScriptError, match="prompts"):
        parse_miou_miou_miou_script(script)


def test_meta_strings_exempt_from_string_cap() -> None:
    detail = "d" * 180
    script = (
        f'meta = {{"name": "x", "description": "ok", '
        f'"phases": [{{"title": "Scan", "detail": "{detail}"}}]}}\n'
        "return None\n"
    )
    parsed = parse_miou_miou_miou_script(script)
    assert parsed.meta.name == "x"


def test_too_many_lines_rejected() -> None:
    script = 'meta = {"name": "x", "description": "d"}\n' + "x = 1\n" * 250
    with pytest.raises(MiouMiouMiouScriptError, match="lines; the cap is"):
        parse_miou_miou_miou_script(script)


def test_all_errors_reported_together() -> None:
    script = (
        'meta = {"name": "x", "description": "d"}\n'
        'result = "oops"\n'
        'value = agent("prompt")\n'
        "import os\n"
    )
    with pytest.raises(MiouMiouMiouScriptError) as exc_info:
        parse_miou_miou_miou_script(script)
    message = str(exc_info.value)
    assert "breaks these rules" in message
    assert "'result' is a miou_miou_miou primitive" in message
    assert "write 'await agent" in message
    assert "imports are unavailable" in message


def test_banned_modules_raise() -> None:
    ns = build_script_globals({"log": lambda _m: None})
    with pytest.raises(MiouMiouMiouScriptError, match="unavailable"):
        ns["time"].time()
    with pytest.raises(MiouMiouMiouScriptError, match="unavailable"):
        ns["random"].random()
    with pytest.raises(MiouMiouMiouScriptError, match="unavailable"):
        ns["datetime"].now()


def test_print_routes_to_log() -> None:
    lines: list[str] = []
    ns = build_script_globals({"log": lines.append})
    ns["__builtins__"]["print"]("hello", 42)
    assert lines == ["hello 42"]


def test_safe_builtins_present_and_dangerous_absent() -> None:
    ns = build_script_globals({})
    builtins_ns = ns["__builtins__"]
    assert "len" in builtins_ns
    assert "sorted" in builtins_ns
    assert "open" not in builtins_ns
    assert "eval" not in builtins_ns
    assert "exec" not in builtins_ns
    assert "__import__" not in builtins_ns
