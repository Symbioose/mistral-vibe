from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Static

from vibe.cli.textual_ui.widgets.braille_renderer import render_braille
from vibe.cli.textual_ui.widgets.no_markup_static import (
    NoMarkupStatic,
    NonSelectableStatic,
)

# Pixel-art grids rendered as braille, in the same style (and with the same
# head) as the banner's PetitChat. '#' is a lit dot.

_KITTEN_GRID = [
    "..............",
    "...#...#......",
    "..#.#.#.#.....",
    "..#..#..#..#..",
    "..#.....#..#..",
    ".##.#.#.#..#..",
    ".#...#...##...",
    "..###.###.#...",
]

_FAT_CAT_GRID = [
    "..#...#.............",
    ".#.#.#.#..####....#.",
    ".#..#..#.#....##..#.",
    ".#.....#........#.#.",
    "#..#.#..#........##.",
    "#........#........#.",
    ".#.......#........#.",
    "..#######.########..",
]


def _render_grid(grid: list[str]) -> str:
    dots = {
        1j * y + x
        for y, row in enumerate(grid)
        for x, char in enumerate(row)
        if char == "#"
    }
    return render_braille(dots, max(len(row) for row in grid), len(grid))


def _scaled(grid: list[str], factor: int) -> list[str]:
    return [
        "".join(char * factor for char in row) for row in grid for _ in range(factor)
    ]


KITTEN_ART = _render_grid(_KITTEN_GRID)
KITTEN_ART_LARGE = _render_grid(_scaled(_KITTEN_GRID, 2))
FAT_CAT_ART = _render_grid(_FAT_CAT_GRID)


class KittenMessage(Static):
    """A kitten that appears when a subagent is dispatched."""

    def __init__(self, agent_name: str, branch: str | None = None) -> None:
        super().__init__()
        self.add_class("subagent-kitten")
        self._agent_name = agent_name
        self._branch = branch
        self._status: str | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(classes="cat-container"):
            yield NonSelectableStatic(KITTEN_ART, classes="cat-art kitten-art")
            yield NoMarkupStatic(self.label_text(), classes="cat-label kitten-label")

    def label_text(self) -> str:
        parts = [self._agent_name, self._branch, self._status]
        return " · ".join(part for part in parts if part)

    def set_status(self, status: str) -> None:
        self._status = status
        if self.is_mounted:
            self.query_one(".kitten-label", NoMarkupStatic).update(self.label_text())


class OrchestratorCatMessage(Static):
    """The fat cat overseeing its kittens while subagents run."""

    def __init__(self) -> None:
        super().__init__()
        self.add_class("orchestrator-cat")
        self._kitten_count = 0

    def compose(self) -> ComposeResult:
        with Horizontal(classes="cat-container"):
            yield NonSelectableStatic(FAT_CAT_ART, classes="cat-art fat-cat-art")
            yield NoMarkupStatic(self.label_text(), classes="cat-label fat-cat-label")

    def label_text(self) -> str:
        noun = "kitten" if self._kitten_count == 1 else "kittens"
        return f"orchestrator · {self._kitten_count} {noun} dispatched"

    def add_kitten(self) -> None:
        self._kitten_count += 1
        if self.is_mounted:
            self.query_one(".fat-cat-label", NoMarkupStatic).update(self.label_text())
