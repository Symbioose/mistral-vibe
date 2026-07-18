from __future__ import annotations


class WorkflowError(Exception):
    pass


class WorkflowScriptError(WorkflowError):
    pass
