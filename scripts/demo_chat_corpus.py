"""Generate the demo-chat corpus: a tiny git project with stubbed modules.

Three modules full of NotImplementedError stubs, each with a complete unittest
file. The demo asks parallel `worker` subagents (isolated worktrees) to
implement one module each; proof of success = `python -m unittest` goes green.

Usage: uv run python scripts/demo_chat_corpus.py --out DIR
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
import shutil
import stat
import subprocess
from typing import Any


def _force_remove(func: Callable[[str], Any], path: str, _exc: object) -> None:
    Path(path).chmod(stat.S_IWRITE)
    func(path)

GEOMETRY = '''\
"""Geometry helpers. Implement every function; tests are in tests/test_geometry.py."""


def rectangle_area(width: float, height: float) -> float:
    """Return width * height. Raise ValueError if either side is negative."""
    raise NotImplementedError


def circle_perimeter(radius: float) -> float:
    """Return 2 * pi * radius (use math.pi). Raise ValueError if radius < 0."""
    raise NotImplementedError


def is_right_triangle(a: float, b: float, c: float) -> bool:
    """Return True if the three sides form a right triangle (any side may be
    the hypotenuse). Compare with a relative tolerance of 1e-9."""
    raise NotImplementedError
'''

TEXT_TOOLS = '''\
"""Text helpers. Implement every function; tests are in tests/test_text_tools.py."""


def reverse_words(sentence: str) -> str:
    """Return the sentence with word order reversed, single-spaced."""
    raise NotImplementedError


def count_vowels(text: str) -> int:
    """Return the number of vowels (aeiou, case-insensitive) in text."""
    raise NotImplementedError


def is_palindrome(text: str) -> bool:
    """Return True if text is a palindrome ignoring case, spaces and
    punctuation (keep only alphanumeric characters)."""
    raise NotImplementedError
'''

SEQUENCES = '''\
"""Sequence helpers. Implement every function; tests are in tests/test_sequences.py."""


def fibonacci(n: int) -> list[int]:
    """Return the first n Fibonacci numbers starting 0, 1. n=0 -> []."""
    raise NotImplementedError


def running_max(values: list[int]) -> list[int]:
    """Return a list where item i is the max of values[0..i]."""
    raise NotImplementedError


def dedupe_keep_order(values: list[int]) -> list[int]:
    """Return values without duplicates, keeping first occurrences in order."""
    raise NotImplementedError
'''

TEST_GEOMETRY = '''\
import math
import unittest

from geometry import circle_perimeter, is_right_triangle, rectangle_area


class TestGeometry(unittest.TestCase):
    def test_rectangle_area(self) -> None:
        self.assertEqual(rectangle_area(3, 4), 12)
        self.assertEqual(rectangle_area(0, 5), 0)
        with self.assertRaises(ValueError):
            rectangle_area(-1, 2)

    def test_circle_perimeter(self) -> None:
        self.assertAlmostEqual(circle_perimeter(1), 2 * math.pi)
        self.assertAlmostEqual(circle_perimeter(0), 0.0)
        with self.assertRaises(ValueError):
            circle_perimeter(-0.1)

    def test_is_right_triangle(self) -> None:
        self.assertTrue(is_right_triangle(3, 4, 5))
        self.assertTrue(is_right_triangle(5, 3, 4))
        self.assertFalse(is_right_triangle(2, 3, 4))


if __name__ == "__main__":
    unittest.main()
'''

TEST_TEXT_TOOLS = '''\
import unittest

from text_tools import count_vowels, is_palindrome, reverse_words


class TestTextTools(unittest.TestCase):
    def test_reverse_words(self) -> None:
        self.assertEqual(reverse_words("le chat dort"), "dort chat le")
        self.assertEqual(reverse_words("meow"), "meow")

    def test_count_vowels(self) -> None:
        self.assertEqual(count_vowels("MeowMeowMeow"), 6)
        self.assertEqual(count_vowels("xyz"), 0)

    def test_is_palindrome(self) -> None:
        self.assertTrue(is_palindrome("Engage le jeu que je le gagne"))
        self.assertFalse(is_palindrome("les chats paralleles"))


if __name__ == "__main__":
    unittest.main()
'''

TEST_SEQUENCES = '''\
import unittest

from sequences import dedupe_keep_order, fibonacci, running_max


class TestSequences(unittest.TestCase):
    def test_fibonacci(self) -> None:
        self.assertEqual(fibonacci(0), [])
        self.assertEqual(fibonacci(1), [0])
        self.assertEqual(fibonacci(7), [0, 1, 1, 2, 3, 5, 8])

    def test_running_max(self) -> None:
        self.assertEqual(running_max([3, 1, 4, 1, 5]), [3, 3, 4, 4, 5])
        self.assertEqual(running_max([]), [])

    def test_dedupe_keep_order(self) -> None:
        self.assertEqual(dedupe_keep_order([3, 1, 3, 2, 1]), [3, 1, 2])


if __name__ == "__main__":
    unittest.main()
'''

README = """\
# demo-chat corpus

Trois modules a implementer (stubs NotImplementedError), un fichier de tests
complet par module. Verification :

    python -m unittest discover -s tests -t . -v

Les tests echouent tant que les modules ne sont pas implementes.
"""

AGENTS_MD = """\
# Regles du projet

- Pour implementer des fonctions manquantes ici, lance des subagents worker EN
  PARALLELE dans un seul tour avec le tool task (agent='worker') : un worker par
  module, chacun isole dans son propre worktree git. N'implemente jamais le code
  toi-meme dans la conversation principale.
- Quand les workers ont fini, merge leurs branches dans main puis lance
  `python -m unittest discover -s tests -t .` et montre le resultat.
"""

FILES = {
    "AGENTS.md": AGENTS_MD,
    ".gitignore": "__pycache__/\n*.pyc\n",
    "tests/__init__.py": "",
    "geometry.py": GEOMETRY,
    "text_tools.py": TEXT_TOOLS,
    "sequences.py": SEQUENCES,
    "tests/test_geometry.py": TEST_GEOMETRY,
    "tests/test_text_tools.py": TEST_TEXT_TOOLS,
    "tests/test_sequences.py": TEST_SEQUENCES,
    "README.md": README,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--no-agents", action="store_true")
    parser.add_argument("--copies", type=int, default=1)
    parsed = parser.parse_args()
    out = Path(parsed.out)
    if out.exists():
        shutil.rmtree(out, onexc=_force_remove)
    (out / "tests").mkdir(parents=True)
    files = dict(FILES)
    suffixes = "bcdefghij"
    for copy_index in range(1, max(1, parsed.copies)):
        suffix = suffixes[copy_index - 1]
        for module in ("geometry", "text_tools", "sequences"):
            files[f"{module}_{suffix}.py"] = files[f"{module}.py"]
            files[f"tests/test_{module}_{suffix}.py"] = files[
                f"tests/test_{module}.py"
            ].replace(f"from {module} import", f"from {module}_{suffix} import")
    for rel, content in files.items():
        if parsed.no_agents and rel == "AGENTS.md":
            continue
        (out / rel).write_text(content, encoding="utf-8", newline="\n")

    def git(*cmd: str) -> None:
        subprocess.run(["git", *cmd], cwd=out, check=True, capture_output=True)

    git("init", "-b", "main")
    git("add", "-A")
    git(
        "-c",
        "user.email=demo@meow.local",
        "-c",
        "user.name=Demo Chat",
        "commit",
        "-m",
        "corpus initial: 9 fonctions a implementer",
    )
    print(f"projet demo-chat pret: {out} (git init, 9 stubs, 9 tests)")


if __name__ == "__main__":
    main()
