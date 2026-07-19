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
    assert len(fat_lines) == 2
    fat_dots = sum(row.count("#") for row in _FAT_CAT_GRID)
    kitten_dots = sum(row.count("#") for row in _KITTEN_GRID)
    assert fat_dots > kitten_dots
    assert max(len(line.rstrip()) for line in fat_lines) > max(
        len(line.rstrip()) for line in KITTEN_ART.splitlines()
    )


def test_cats_stay_compact_for_the_transcript():
    assert len(KITTEN_ART.splitlines()) == 2
    assert len(FAT_CAT_ART.splitlines()) == 2
    assert max(len(line) for line in FAT_CAT_ART.splitlines()) <= 12
