"""Evidence classification for Git-Blast (Track 2: "graph is evidence, not an oracle").

Every relation the code graph reports, and every test selection derived from it,
is tagged with one of three classes so users and agents can tell apart:

* ``confirmed``  — a structural fact: the graph resolved the relation exactly, at
  high confidence, in a semantically-parsed language, with no warning codes.
* ``heuristic``  — the graph produced it but flagged it weak: a name-only or
  import-level resolution, mid confidence, a warning code, or a match that came
  from convention rather than the graph at all.
* ``unverified`` — the graph could not resolve it, or the snapshot itself is
  partial: a parse failure, an inventory-only language, a degraded completeness
  level, or a reachability path that runs through a ``heuristic`` edge and then
  loses the trail (dynamic dispatch, generated code, reflection).

The thresholds live here as named constants. They are calibrated for the
entire-graph provider ``v0.4.0`` snapshot schema (``schema_features`` includes
``relation_resolution`` / ``relation_evidence``) and are overridable from the
CLI via ``--min-confidence``. When a snapshot predates those schema features the
classifier has nothing to grade on and falls back to ``heuristic``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Sequence

# --- the vocabulary ----------------------------------------------------------

CONFIRMED = "confirmed"
HEURISTIC = "heuristic"
UNVERIFIED = "unverified"

CLASSES = (CONFIRMED, HEURISTIC, UNVERIFIED)

# Higher = weaker. ``weakest()`` keeps the maximum along a path.
_ORDER = {CONFIRMED: 0, HEURISTIC: 1, UNVERIFIED: 2}

# --- thresholds (provider v0.4.0) -----------------------------------------

# ``confidence`` at or above this, with a resolved edge and no warnings, is
# treated as a structural fact. Healthy internal Python imports land at
# 0.88-0.95 with ``resolution: import_resolved`` — that is the normal
# fully-resolved case and must clear this bar.
CONFIDENCE_CONFIRMED = 0.85
# Below this the relation is too weak to act on without source/test proof.
CONFIDENCE_UNVERIFIED_BELOW = 0.5

# ``resolution`` values that mean the provider actually tied the edge to a
# concrete target file/symbol (as opposed to guessing by name).
RESOLVED_RESOLUTIONS = frozenset({"exact", "import_resolved"})
# ``resolution`` values that are explicitly approximate.
WEAK_RESOLUTIONS = frozenset({"name_only", "import_external", "heuristic", "fuzzy"})

# Marker used when a mapping comes from the convention matcher, not the graph.
ORIGIN_CONVENTION = "convention"

INVENTORY_ONLY_TIER = "inventory-only"
SEMANTIC_TIER = "semantic"


def weakest(classes: Iterable[str]) -> str:
    """Return the weakest (least trustworthy) class in ``classes``.

    Empty input is treated as ``confirmed`` — there is nothing dragging it down.
    """
    worst = CONFIRMED
    for cls in classes:
        if _ORDER.get(cls, 1) > _ORDER[worst]:
            worst = cls
    return worst


def is_weaker(a: str, b: str) -> bool:
    """True when ``a`` is strictly less trustworthy than ``b``."""
    return _ORDER.get(a, 1) > _ORDER.get(b, 1)


# --- relation classification -------------------------------------------------

def classify_relation(
    rel: dict,
    *,
    language_tier: str | None = None,
    snapshot_partial: bool = False,
    resolution_supported: bool = True,
    confidence_confirmed: float = CONFIDENCE_CONFIRMED,
) -> str:
    """Grade one ``relation`` record from the snapshot.

    ``rel`` carries the provider fields ``confidence`` (0-1), ``resolution``,
    ``relation_scope``, ``target_kind`` and ``warning_codes``.  ``language_tier``
    is the tier of the *source* file's language (``semantic`` /
    ``inventory-only``).  ``snapshot_partial`` is set when the snapshot as a
    whole reported partial failures or a degraded completeness level.
    """
    if snapshot_partial:
        return UNVERIFIED
    if language_tier == INVENTORY_ONLY_TIER:
        return UNVERIFIED

    warning_codes = rel.get("warning_codes") or []
    resolution = rel.get("resolution")
    confidence = rel.get("confidence")

    # Snapshot too old to carry grading signal: nothing to confirm on.
    if not resolution_supported or (resolution is None and confidence is None):
        return HEURISTIC

    if confidence is not None and confidence < CONFIDENCE_UNVERIFIED_BELOW:
        return UNVERIFIED

    if warning_codes:
        return HEURISTIC

    if resolution in WEAK_RESOLUTIONS:
        return HEURISTIC

    if resolution in RESOLVED_RESOLUTIONS and (
        confidence is None or confidence >= confidence_confirmed
    ):
        return CONFIRMED

    # A resolved edge whose confidence dipped below the bar, or an unrecognised
    # resolution string: not a fact, not unresolved.
    return HEURISTIC


def confidence_label(surface_class: str) -> str:
    """Map the weakest evidence class on a selection to a ``confidence`` label."""
    return {
        CONFIRMED: "high",
        HEURISTIC: "medium",
        UNVERIFIED: "low",
    }.get(surface_class, "medium")


# --- dynamic-dispatch source scan -----------------------------------------

# Advisory only: a hit downgrades a file's evidence class and can trigger the
# fallback ladder, but never hides a test that was already selected.
_MARKER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("getattr", re.compile(r"\bgetattr\s*\(")),
    ("setattr", re.compile(r"\bsetattr\s*\(")),
    ("__import__", re.compile(r"\b__import__\s*\(")),
    ("importlib", re.compile(r"\bimportlib\.(import_module|__import__)\b")),
    ("globals-index", re.compile(r"\bglobals\s*\(\s*\)\s*\[")),
    ("locals-index", re.compile(r"\blocals\s*\(\s*\)\s*\[")),
    ("entry_points", re.compile(r"\bentry_points\s*\(")),
    ("pkg_resources", re.compile(r"\bpkg_resources\b")),
    ("load_entry_point", re.compile(r"\bload_entry_point\b")),
    ("pluggy", re.compile(r"\bpluggy\b")),
    ("registry-register", re.compile(r"\.register\s*\(")),
)

# Files bigger than this are skipped by the scan (generated blobs, vendored code).
_MAX_SCAN_BYTES = 512 * 1024


def scan_dynamic_dispatch(
    repo_root: str,
    files: Sequence[str],
) -> dict[str, list[str]]:
    """Return ``{path: [marker_name, ...]}`` for files containing dynamic-dispatch
    constructs static analysis cannot follow.

    Best-effort: unreadable, oversized, or non-text files are skipped silently.
    """
    root = Path(repo_root)
    hits: dict[str, list[str]] = {}
    for rel_path in files:
        if not rel_path or not rel_path.endswith(".py"):
            continue
        target = root / rel_path
        try:
            if not target.is_file() or target.stat().st_size > _MAX_SCAN_BYTES:
                continue
            text = target.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        found = [name for name, pat in _MARKER_PATTERNS if pat.search(text)]
        if found:
            hits[rel_path] = found
    return hits
