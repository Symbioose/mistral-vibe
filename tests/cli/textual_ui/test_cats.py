from __future__ import annotations

from vibe.cli.textual_ui.widgets.cats import (
    _FAT_CAT_GRID,
    _KITTEN_GRID,
    FAT_CAT_ART,
    KITTEN_ART,
)


def test_kitten_art_is_compact_braille():
    lines = KITTEN_ART.splitlines()
    assert len(lines) == 2
    assert all(0x2800 <= ord(c) <= 0x28FF or c == " " for line in lines for c in line)
    assert any(c != " " for line in lines for c in line)


def test_fat_cat_art_is_bigger_than_the_kitten():
    fat_lines = FAT_CAT_ART.splitlines()
    assert len(fat_lines) == 3
    fat_dots = sum(row.count("#") for row in _FAT_CAT_GRID)
    kitten_dots = sum(row.count("#") for row in _KITTEN_GRID)
    assert fat_dots > kitten_dots


def test_both_cats_share_the_banner_head():
    # Same face (ears, eyes, chin) so the cat family is visually consistent.
    kitten_head = [row[:10] for row in _KITTEN_GRID[:8]]
    fat_head = [row[:10] for row in _FAT_CAT_GRID[:8]]
    assert kitten_head[:5] == fat_head[:5]
