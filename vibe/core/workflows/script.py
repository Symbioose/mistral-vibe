from __future__ import annotations

import ast
import builtins
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
        raise WorkflowScriptError(f"script has a syntax error: {e}") from e

    meta = _extract_meta(tree)
    _check_forbidden_constructs(tree)

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


def _check_forbidden_constructs(tree: ast.Module) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise WorkflowScriptError(
                "imports are unavailable in workflow scripts; "
                "json, math and re are pre-loaded"
            )
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise WorkflowScriptError(
                "dunder attribute access is unavailable in workflow scripts"
            )
        if isinstance(node, ast.Name) and node.id in {"__builtins__", "__import__"}:
            raise WorkflowScriptError(f"{node.id} is unavailable in workflow scripts")
