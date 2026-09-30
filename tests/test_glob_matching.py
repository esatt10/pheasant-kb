"""`_match_any` compiles its globs and must still mean exactly what it meant.

It decides which files every connector, the walker and the watcher take, so a
difference from the reference is a file silently indexed or silently dropped.
The reference is the implementation it replaced -- two ``fnmatch`` calls per
pattern -- run against the shipped include/exclude lists and against globs
chosen to break a naive translation: unbalanced brackets, regex
metacharacters, negated classes, ``**``, empty patterns and empty paths.
"""

from __future__ import annotations

import fnmatch
import random
import string

import pytest

from pheasant.config.schema import SourceConfig
from pheasant.ingestion.pipeline import _match_any


def _reference(relative: str, patterns: list[str]) -> bool:
    return any(
        fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch("/" + relative, pattern)
        for pattern in patterns
    )


_DEFAULTS = SourceConfig(name="x", type="repository", path="/tmp")

_HOSTILE = [
    "*.py",
    "**/*.md",
    "/abs/*",
    "a?c",
    "[!x]*.txt",
    "[a-c]/**",
    "**",
    "*",
    "",
    "weird[",
    "docs/**/api/*",
    "*.[ch]",
    "**/.git/**",
    "x.(y)|z",
    "a+b*",
    "^$.*",
]

_PATHS = [
    "src/a.py",
    "docs/x/api/y.md",
    ".git/config",
    "node_modules/pkg/index.js",
    "a/b/c.txt",
    "abs/q",
    "abc",
    "xy.txt",
    "b/q",
    "",
    "/",
    "x.(y)|z",
    "a+bbb",
    "^$.q",
    "a\nb.py",
    "dir.with.dots/f",
    "A.PY",
]


@pytest.mark.parametrize("path", _PATHS)
def test_the_shipped_globs_decide_every_path_as_before(path: str) -> None:
    for patterns in (list(_DEFAULTS.include), list(_DEFAULTS.exclude), _HOSTILE, []):
        assert _match_any(path, patterns) == _reference(path, patterns), (path, patterns)


def test_random_paths_and_pattern_subsets_agree_with_fnmatch() -> None:
    rng = random.Random(7)
    alphabet = string.ascii_lowercase[:6] + "./_-[]()|+^$*?!"
    pool = list(_DEFAULTS.include) + list(_DEFAULTS.exclude) + _HOSTILE
    for _ in range(4000):
        path = "/".join(
            "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 6)))
            for _ in range(rng.randint(1, 4))
        )
        patterns = rng.sample(pool, rng.randint(1, 5))
        assert _match_any(path, patterns) == _reference(path, patterns), (path, patterns)


def test_any_iterable_of_patterns_is_accepted() -> None:
    """Callers pass lists, tuples and one-off generators; all mean the same."""

    assert _match_any("docs/a.md", ["**/*.md"])
    assert _match_any("docs/a.md", ("**/*.md",))
    assert _match_any("docs/a.md", (pattern for pattern in ["**/*.md"]))
    assert not _match_any("docs/a.md", iter([]))
