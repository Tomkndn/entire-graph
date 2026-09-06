"""Convention-based ``src`` → ``test`` matching (SPEC.md "matcher.py").

Used as a fallback when the graph / database produces nothing for a modified
file. Given a source path and the set of test files that exist in the repo, it
applies five ordered strategies and returns the union, most-confident first:

1. exact basename    — ``test_<stem>.py`` / ``<stem>_test.py`` anywhere
2. sibling test dir  — ``<dir>/tests|test/test_<stem>.py``
3. mirrored path     — top-level ``src`` swapped for ``tests``/``test``, or a
                       ``tests/`` prefix, keeping the sub-path
4. package __init__  — ``__init__.py`` matched via its package directory name
5. token prefix      — ``test_<stem>_*.py`` / ``<stem>_*_test.py`` (loose)

Everything is deduplicated while preserving first-seen (highest-confidence)
order.
"""

from __future__ import annotations

import os
import posixpath
from typing import Iterable

_TEST_DIR_NAMES = ("tests", "test")


def _norm(path: str) -> str:
    return path.replace(os.sep, "/").lstrip("./")


def looks_like_test_file(path: str) -> bool:
    """True only for runnable pytest modules: ``test_*.py`` / ``*_test.py``.

    A helper like ``tests/utils.py`` or ``tests/conftest.py`` lives in the test
    tree but is not a test to execute — use :func:`in_test_tree` for that.
    """
    p = _norm(path)
    if not p.endswith(".py"):
        return False
    base = posixpath.basename(p)
    return base.startswith("test_") or base.endswith("_test.py")


def in_test_tree(path: str) -> bool:
    """True for anything that belongs to the test suite: a test module, a
    ``conftest.py``, or any ``.py`` under a ``tests/`` / ``test/`` directory."""
    p = _norm(path)
    if not p.endswith(".py"):
        return False
    if looks_like_test_file(p) or posixpath.basename(p) == "conftest.py":
        return True
    return any(seg in _TEST_DIR_NAMES for seg in p.split("/")[:-1])


def source_stem(source_file: str) -> str:
    """The identifying stem of a source file (``__init__`` -> package dir)."""
    p = _norm(source_file)
    base = posixpath.basename(p)
    stem = base[:-3] if base.endswith(".py") else base
    if stem == "__init__":
        parent = posixpath.dirname(p)
        return posixpath.basename(parent) or "__init__"
    return stem


def _basename_variants(stem: str) -> set[str]:
    return {f"test_{stem}.py", f"{stem}_test.py"}


def match_tests(source_file: str, candidate_tests: Iterable[str]) -> list[str]:
    """Ordered, deduplicated test files that conventionally cover ``source_file``."""
    src = _norm(source_file)
    if in_test_tree(src):
        return []

    candidates = [_norm(c) for c in candidate_tests if _norm(c).endswith(".py")]
    candidate_set = set(candidates)
    stem = source_stem(src)
    src_dir = posixpath.dirname(src)
    exact = _basename_variants(stem)

    ordered: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        if path in candidate_set and path not in seen:
            seen.add(path)
            ordered.append(path)

    # 1. exact basename anywhere
    for c in candidates:
        if posixpath.basename(c) in exact:
            add(c)

    # 2. sibling tests/ or test/ dir
    for test_dir in _TEST_DIR_NAMES:
        for name in exact:
            add(posixpath.join(src_dir, test_dir, name) if src_dir else posixpath.join(test_dir, name))

    # 3. mirrored path
    parts = src.split("/")
    sub = parts[1:] if len(parts) > 1 else parts
    sub_dir = "/".join(sub[:-1])
    for prefix in _TEST_DIR_NAMES:
        for name in exact:
            add(posixpath.join(prefix, sub_dir, name) if sub_dir else posixpath.join(prefix, name))
    # also a plain tests/ prefix over the full path
    for prefix in _TEST_DIR_NAMES:
        for name in exact:
            add(posixpath.join(prefix, src_dir, name) if src_dir else posixpath.join(prefix, name))

    # 4. package __init__.py already handled by source_stem(); nothing extra.

    # 5. token prefix / loose (only when the stem is distinctive enough)
    if len(stem) >= 3:
        for c in candidates:
            cbase = posixpath.basename(c)[:-3]
            tokens = set(cbase.replace("-", "_").split("_"))
            if stem in tokens:
                add(c)

    return ordered


def match_surface(
    import_surface: Iterable[str], candidate_tests: Iterable[str]
) -> list[str]:
    """Union of :func:`match_tests` over every non-test file in ``import_surface``."""
    candidates = list(candidate_tests)
    out: list[str] = []
    seen: set[str] = set()
    for source in import_surface:
        if in_test_tree(source):
            continue
        for t in match_tests(source, candidates):
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out
