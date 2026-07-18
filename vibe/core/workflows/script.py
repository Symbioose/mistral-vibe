from __future__ import annotations

import ast
import builtins
from collections.abc import Iterator
from dataclasses import dataclass
import json
import math
import re
import types
from typing import Any

from pydantic import ValidationError

from vibe.core.workflows.errors import WorkflowScriptError
from vibe.core.workflows.models import WorkflowMeta

WORKFLOW_MAIN_NAME = "__workflow_main__"
_OFFENDING_LINE_MAX_LEN = 120

_ALLOWED_BUILTIN_NAMES = (
    "abs",
    "all",
    "any",
    "bool",
    "callable",
    "chr",
    "dict",
    "divmod",
    "enumerate",
    "filter",
    "float",
    "format",
    "frozenset",
    "getattr",
    "hasattr",
    "hash",
    "int",
    "isinstance",
    "issubclass",
    "iter",
    "len",
    "list",
    "map",
    "max",
    "min",
    "next",
    "ord",
    "pow",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "slice",
    "sorted",
    "str",
    "sum",
    "tuple",
    "zip",
    "ArithmeticError",
    "AttributeError",
    "BaseException",
    "Exception",
    "IndexError",
    "KeyError",
    "LookupError",
    "RuntimeError",
    "StopAsyncIteration",
    "StopIteration",
    "TypeError",
    "ValueError",
    "ZeroDivisionError",
    "True",
    "False",
    "None",
)

_BANNED_MODULE_REASONS = {
    "time": "wall-clock time would break workflow resume; pass timestamps in via args",
    "datetime": "wall-clock time would break workflow resume; pass timestamps in via args",
    "random": "nondeterminism would break workflow resume; vary agent prompts by index instead",
    "os": "workflow scripts have no filesystem access; subagents do the real-world work",
    "sys": "workflow scripts have no interpreter access; subagents do the real-world work",
}


class _BannedModule:
    def __init__(self, name: str, reason: str) -> None:
        self._name = name
        self._reason = reason

    def __getattr__(self, item: str) -> Any:
        raise WorkflowScriptError(
            f"{self._name}.{item} is unavailable in workflow scripts: {self._reason}"
        )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise WorkflowScriptError(
            f"{self._name} is unavailable in workflow scripts: {self._reason}"
        )


@dataclass(frozen=True)
class ParsedScript:
    meta: WorkflowMeta
    code: types.CodeType
    source: str


def parse_workflow_script(source: str) -> ParsedScript:
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        raise WorkflowScriptError(_format_syntax_error(source, e)) from e

    errors: list[str] = []
    meta: WorkflowMeta | None = None
    try:
        meta = _extract_meta(tree)
    except WorkflowScriptError as e:
        errors.append(str(e))
    errors.extend(_collect_violations(tree))
    if errors:
        if len(errors) == 1:
            raise WorkflowScriptError(errors[0])
        raise WorkflowScriptError(
            "the script breaks these rules:\n- " + "\n- ".join(errors)
        )
    if meta is None:
        raise WorkflowScriptError("invalid meta")

    body = tree.body[1:] or [ast.Pass()]
    wrapper = ast.AsyncFunctionDef(
        name=WORKFLOW_MAIN_NAME,
        args=ast.arguments(
            posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]
        ),
        body=body,
        decorator_list=[],
        returns=None,
        type_params=[],
    )
    module = ast.Module(body=[wrapper], type_ignores=[])
    ast.fix_missing_locations(module)
    code = compile(module, filename=f"<workflow:{meta.name}>", mode="exec")
    return ParsedScript(meta=meta, code=code, source=source)


def _format_syntax_error(source: str, e: SyntaxError) -> str:
    message = f"script has a syntax error: {e.msg} (script line {e.lineno})"
    lines = source.splitlines()
    if e.lineno is not None and 1 <= e.lineno <= len(lines):
        offending = lines[e.lineno - 1].strip()
        if len(offending) > _OFFENDING_LINE_MAX_LEN:
            offending = offending[: _OFFENDING_LINE_MAX_LEN - 1] + "…"
        message += f"\n  offending line: {offending!r}"
    if "string literal" in (e.msg or "") or "EOF" in (e.msg or ""):
        message += (
            "\n  Tip: do not embed long prose prompts as Python strings — pass them "
            "in the `prompts` tool argument (a JSON object) and reference them as "
            'prompts["key"] in the script.'
        )
    return message


def build_script_globals(primitives: dict[str, Any]) -> dict[str, Any]:
    safe_builtins = {
        name: getattr(builtins, name)
        for name in _ALLOWED_BUILTIN_NAMES
        if hasattr(builtins, name)
    }
    safe_builtins["print"] = _make_print(primitives.get("log"))
    ns: dict[str, Any] = {
        "__builtins__": safe_builtins,
        "json": json,
        "math": math,
        "re": re,
    }
    for name, reason in _BANNED_MODULE_REASONS.items():
        ns[name] = _BannedModule(name, reason)
    ns.update(primitives)
    return ns


def _make_print(log: Any) -> Any:
    def _print(*values: Any, sep: str = " ", **_kwargs: Any) -> None:
        if log is not None:
            log(sep.join(str(v) for v in values))

    return _print


def _extract_meta(tree: ast.Module) -> WorkflowMeta:
    first = tree.body[0] if tree.body else None
    if (
        not isinstance(first, ast.Assign)
        or len(first.targets) != 1
        or not isinstance(first.targets[0], ast.Name)
        or first.targets[0].id != "meta"
    ):
        raise WorkflowScriptError(
            'script must start with `meta = {"name": ..., "description": ...}`'
        )
    try:
        raw = ast.literal_eval(first.value)
    except ValueError as e:
        raise WorkflowScriptError(
            "meta must be a pure literal dict (no variables, calls, or comprehensions)"
        ) from e
    if not isinstance(raw, dict):
        raise WorkflowScriptError("meta must be a dict literal")
    try:
        return WorkflowMeta.model_validate(raw)
    except ValidationError as e:
        raise WorkflowScriptError(f"invalid meta: {e}") from e


RESERVED_PRIMITIVES = frozenset({
    "agent",
    "parallel",
    "pipeline",
    "phase",
    "log",
    "result",
    "args",
    "prompts",
})


_AWAITABLE_PRIMITIVES = frozenset({"agent", "parallel", "pipeline"})


def _collect_violations(tree: ast.Module) -> list[str]:
    errors: list[str] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", None)
        location = f" (script line {line})" if line else ""
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            errors.append(
                f"imports are unavailable in workflow scripts{location}; "
                "json, math and re are pre-loaded"
            )
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            errors.append(
                f"dunder attribute access is unavailable in workflow scripts{location}"
            )
        elif isinstance(node, ast.Name) and node.id in {"__builtins__", "__import__"}:
            errors.append(f"{node.id} is unavailable in workflow scripts{location}")
        else:
            shadowed = _shadowed_primitive(node)
            if shadowed is not None:
                errors.append(
                    f"'{shadowed}' is a workflow primitive and cannot be used as a "
                    f"variable, parameter, or function name{location} — rename it "
                    f"(e.g. '{shadowed}_value')"
                )
    errors.extend(_collect_missing_awaits(tree))
    return errors


def _shadowed_primitive(node: ast.AST) -> str | None:
    match node:
        case ast.Name(id=name, ctx=ast.Store()) if name in RESERVED_PRIMITIVES:
            return name
        case ast.arg(arg=name) if name in RESERVED_PRIMITIVES:
            return name
        case (
            ast.FunctionDef(name=name)
            | ast.AsyncFunctionDef(name=name)
            | ast.ClassDef(name=name)
        ) if name in RESERVED_PRIMITIVES:
            return name
    return None


def _collect_missing_awaits(tree: ast.Module) -> list[str]:
    errors: list[str] = []
    for node in _iter_async_context(tree):
        value: ast.expr | None = None
        match node:
            case ast.Expr(value=candidate) | ast.Return(value=candidate):
                value = candidate
            case ast.Assign(value=candidate) | ast.AnnAssign(value=candidate):
                value = candidate
        name = _bare_primitive_call(value)
        if name is not None:
            lineno = getattr(node, "lineno", 0)
            errors.append(
                f"'{name}(...)' returns an awaitable and its result is used "
                f"without await (script line {lineno}) — "
                f"write 'await {name}(...)'"
            )
    return errors


def _iter_async_context(node: ast.AST) -> Iterator[ast.AST]:
    # Sync defs and lambdas are thunk factories: a bare agent() call there is
    # intentional (parallel()/pipeline() await it). Everything else runs in the
    # top-level async context.
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.Lambda)):
            continue
        yield child
        yield from _iter_async_context(child)


def _bare_primitive_call(value: ast.expr | None) -> str | None:
    match value:
        case ast.Call(func=ast.Name(id=name)) if name in _AWAITABLE_PRIMITIVES:
            return name
    return None
