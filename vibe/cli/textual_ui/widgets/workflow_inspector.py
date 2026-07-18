from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static, Tree
from textual.widgets.tree import TreeNode

from vibe.cli.textual_ui.widgets.no_markup_static import NoMarkupStatic
from vibe.cli.textual_ui.widgets.status_message import IndicatorState
from vibe.cli.textual_ui.widgets.workflow import WorkflowAgentRow, WorkflowCallMessage

_SYNC_INTERVAL_S = 0.5

_STATE_GLYPHS = {
    IndicatorState.SUCCESS: "✓",
    IndicatorState.ERROR: "✕",
    IndicatorState.MUTED: "□",
}


class WorkflowInspectorScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss_inspector", "Close", priority=True),
        Binding("q", "dismiss_inspector", "Close", show=False),
        Binding("ctrl+w", "dismiss_inspector", "Close", show=False),
    ]

    def __init__(self, workflow: WorkflowCallMessage) -> None:
        super().__init__()
        self._workflow = workflow
        self._tree: Tree[int] | None = None
        self._detail: VerticalScroll | None = None
        self._phase_nodes: dict[str | None, TreeNode[int]] = {}
        self._agent_nodes: dict[int, TreeNode[int]] = {}
        self._selected_agent: int | None = None
        self._rendered_log_len = -1
        self._rendered_finished: bool | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="workflow-inspector"):
            yield NoMarkupStatic(
                "Workflow inspector — ↑/↓ navigate · esc close",
                id="workflow-inspector-title",
            )
            with Horizontal(id="workflow-inspector-body"):
                self._tree = Tree("workflow", id="workflow-inspector-tree")
                self._tree.show_root = False
                self._tree.guide_depth = 2
                yield self._tree
                self._detail = VerticalScroll(id="workflow-inspector-detail")
                yield self._detail

    def on_mount(self) -> None:
        self._sync()
        if self._tree is not None:
            self._tree.focus()
            first = next(iter(self._agent_nodes.values()), None)
            if first is not None:
                self._tree.select_node(first)
        self.set_interval(_SYNC_INTERVAL_S, self._sync)

    def action_dismiss_inspector(self) -> None:
        self.dismiss(None)

    def _row_glyph(self, row: WorkflowAgentRow) -> str:
        if not row.finished:
            return "◐"
        return _STATE_GLYPHS.get(row._state, "·")

    def _node_label(self, row: WorkflowAgentRow) -> str:
        return f"{self._row_glyph(row)} {row.get_content().lstrip('▸▾ ')}"

    def _sync(self) -> None:
        if self._tree is None:
            return
        for phase in self._workflow.phase_order:
            if phase not in self._phase_nodes:
                title = phase if phase is not None else "(no phase)"
                self._phase_nodes[phase] = self._tree.root.add(
                    f"◆ {title}", expand=True
                )
        for agent_id, row in self._workflow.agent_rows.items():
            node = self._agent_nodes.get(agent_id)
            if node is None:
                parent = self._phase_nodes.get(row.phase_title)
                if parent is None:
                    parent = self._tree.root.add(
                        "◆ (no phase)"
                        if row.phase_title is None
                        else f"◆ {row.phase_title}",
                        expand=True,
                    )
                    self._phase_nodes[row.phase_title] = parent
                node = parent.add_leaf(self._node_label(row), data=agent_id)
                self._agent_nodes[agent_id] = node
            else:
                node.set_label(self._node_label(row))
        self._refresh_detail()

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted[int]) -> None:
        if event.node.data is not None:
            self._selected_agent = event.node.data
            self._rendered_log_len = -1
            self._refresh_detail()

    def _refresh_detail(self) -> None:
        if self._detail is None or self._selected_agent is None:
            return
        row = self._workflow.agent_rows.get(self._selected_agent)
        if row is None:
            return
        unchanged = (
            len(row.activity_log) == self._rendered_log_len
            and row.finished == self._rendered_finished
        )
        if unchanged:
            return
        self._rendered_log_len = len(row.activity_log)
        self._rendered_finished = row.finished
        self._detail.remove_children()
        header = f"{self._row_glyph(row)} {row.get_content().lstrip('▸▾ ')}"
        if row.status_detail:
            header += f"\n{row.status_detail}"
        self._detail.mount(Static(header, classes="workflow-inspector-header"))
        self._detail.mount(
            NoMarkupStatic("── prompt ──", classes="workflow-inspector-section")
        )
        self._detail.mount(
            NoMarkupStatic(
                row.prompt or "(prompt unavailable)",
                classes="workflow-inspector-prompt",
            )
        )
        self._detail.mount(
            NoMarkupStatic(
                f"── activity ({len(row.activity_log)}) ──",
                classes="workflow-inspector-section",
            )
        )
        if row.activity_log:
            self._detail.mount(
                NoMarkupStatic(
                    "\n".join(row.activity_log), classes="workflow-inspector-activity"
                )
            )
        else:
            self._detail.mount(
                NoMarkupStatic(
                    "(no activity yet)", classes="workflow-inspector-activity"
                )
            )
