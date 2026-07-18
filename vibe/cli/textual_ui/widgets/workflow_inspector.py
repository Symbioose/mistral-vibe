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
        Binding("f", "toggle_follow", "Follow", show=False),
    ]

    def __init__(
        self, workflow: WorkflowCallMessage, initial_agent: int | None = None
    ) -> None:
        super().__init__()
        self._workflow = workflow
        self._initial_agent = initial_agent
        self._follow = initial_agent is None
        self._tree: Tree[int] | None = None
        self._detail: VerticalScroll | None = None
        self._title: NoMarkupStatic | None = None
        self._phase_nodes: dict[str | None, TreeNode[int]] = {}
        self._agent_nodes: dict[int, TreeNode[int]] = {}
        self._selected_agent: int | None = None
        self._rendered_log_len = -1
        self._rendered_finished: bool | None = None
        self._expected_selection: int | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="workflow-inspector"):
            self._title = NoMarkupStatic("", id="workflow-inspector-title")
            yield self._title
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
            target = None
            if self._initial_agent is not None:
                target = self._agent_nodes.get(self._initial_agent)
            if target is None:
                target = next(iter(self._agent_nodes.values()), None)
            if target is not None:
                self._select_node(target)
        self.set_interval(_SYNC_INTERVAL_S, self._sync)

    def action_dismiss_inspector(self) -> None:
        self.dismiss(None)

    def action_toggle_follow(self) -> None:
        self._follow = not self._follow
        self._update_title()
        if self._follow:
            self._follow_latest()

    def _select_node(self, node: TreeNode[int]) -> None:
        if self._tree is None:
            return
        if node.data is not None:
            self._expected_selection = node.data
            self._selected_agent = node.data
            self._rendered_log_len = -1
            self._refresh_detail()
        self._tree.select_node(node)

    def _row_glyph(self, row: WorkflowAgentRow) -> str:
        if not row.finished:
            return "◐"
        return _STATE_GLYPHS.get(row.indicator_state, "·")

    def _node_label(self, row: WorkflowAgentRow) -> str:
        return f"{self._row_glyph(row)} {row.get_content().lstrip('▸ ')}"

    def _phase_label(self, phase: str | None) -> str:
        title = phase if phase is not None else "(no phase)"
        rows = [r for r in self._workflow.agent_rows.values() if r.phase_title == phase]
        done = sum(r.finished for r in rows)
        return f"◆ {title} — {done}/{len(rows)}"

    def _update_title(self) -> None:
        if self._title is None:
            return
        rows = self._workflow.agent_rows
        done = sum(r.finished for r in rows.values())
        follow = "on" if self._follow else "off"
        self._title.update(
            f"Workflow inspector — {done}/{len(rows)} agents · "
            f"↑/↓ navigate · f follow: {follow} · esc close"
        )

    def _sync(self) -> None:
        if self._tree is None:
            return
        for phase in self._workflow.phase_order:
            if phase not in self._phase_nodes:
                self._phase_nodes[phase] = self._tree.root.add(
                    self._phase_label(phase), expand=True
                )
        for phase, node in self._phase_nodes.items():
            node.set_label(self._phase_label(phase))
        for agent_id, row in self._workflow.agent_rows.items():
            node = self._agent_nodes.get(agent_id)
            if node is None:
                parent = self._phase_nodes.get(row.phase_title)
                if parent is None:
                    parent = self._tree.root.add(
                        self._phase_label(row.phase_title), expand=True
                    )
                    self._phase_nodes[row.phase_title] = parent
                node = parent.add_leaf(self._node_label(row), data=agent_id)
                self._agent_nodes[agent_id] = node
            else:
                node.set_label(self._node_label(row))
        self._update_title()
        if self._follow:
            self._follow_latest()
        self._refresh_detail()

    def _follow_latest(self) -> None:
        running = [
            agent_id
            for agent_id, row in self._workflow.agent_rows.items()
            if not row.finished
        ]
        target_id = max(running) if running else max(self._agent_nodes, default=None)
        if target_id is None or target_id == self._selected_agent:
            return
        node = self._agent_nodes.get(target_id)
        if node is not None:
            self._select_node(node)

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted[int]) -> None:
        if event.node.data is None:
            return
        if event.node.data == self._expected_selection:
            self._expected_selection = None
        else:
            self._follow = False
            self._update_title()
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
        header = f"{self._row_glyph(row)} {row.get_content().lstrip('▸ ')}"
        if row.status_detail:
            header += f"\n{row.status_detail}"
        self._detail.mount(Static(header, classes="workflow-inspector-header"))
        self._mount_section("prompt", row.prompt or "(prompt unavailable)")
        activity = (
            "\n".join(row.activity_log) if row.activity_log else "(no activity yet)"
        )
        self._mount_section(f"activity ({len(row.activity_log)})", activity)
        if row.finished:
            self._mount_section("output", row.output or "(no output)")

    def _mount_section(self, title: str, body: str) -> None:
        if self._detail is None:
            return
        self._detail.mount(
            NoMarkupStatic(f"── {title} ──", classes="workflow-inspector-section")
        )
        self._detail.mount(NoMarkupStatic(body, classes="workflow-inspector-body-text"))
