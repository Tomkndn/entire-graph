"""Graph + git-diff analysis for Git-Blast Live (SPEC.md "parser.py").

Pipeline:

1. ``get_modified_files()``          — what git says changed (staged + unstaged
   + untracked ``.py``).
2. ``parse_entire_graph_snapshot()`` — the file/IMPORTS graph from
   ``entire graph snapshot``.
3. ``get_modified_import_surface()`` — walk reverse ``IMPORTS`` edges out from
   the modified files to every file that could be affected.
4. ``get_graph_for_dashboard()``    — the same graph shaped for React Flow.

Only ``IMPORTS`` relations are used for impact analysis, and only the internal
file→file ones (``external:import:*`` targets are ignored).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from . import evidence

DEFAULT_ENTIRE_BIN = os.environ.get("GIT_BLAST_ENTIRE_BIN", "entire")
SNAPSHOT_TIMEOUT_SECONDS = 300

_FILE_MARKER = ":file:"


class SnapshotError(RuntimeError):
    """Raised when ``entire graph snapshot`` cannot be run or parsed."""


@dataclass
class FileNode:
    id: str
    path: str
    language: str | None = None
    imports: set[str] = field(default_factory=set)      # file ids this file imports
    imported_by: set[str] = field(default_factory=set)  # file ids that import this file
    language_tier: str | None = None                    # semantic / inventory-only
    parsed: bool = True                                 # False if in partial_failures


@dataclass
class Graph:
    """Parsed ``entire graph snapshot`` restricted to files + internal IMPORTS."""

    files: dict[str, FileNode] = field(default_factory=dict)
    relations: list[dict] = field(default_factory=list)
    # TESTS relations, resolved to file paths: {test_file, source_file, source_symbol}
    test_edges: list[dict] = field(default_factory=list)
    repo_root: str | None = None
    repo_key: str | None = None
    commit: str | None = None

    # -- Track 2: analysis-completeness signal from the snapshot -------
    completeness_level: str = "ok"
    partial_failures: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    language_tiers: dict[str, str] = field(default_factory=dict)
    schema_features: list[str] = field(default_factory=list)
    unparsed_files: list[str] = field(default_factory=list)
    # {(importer_path, imported_path): evidence class} for internal IMPORTS edges
    edge_class: dict[tuple[str, str], str] = field(default_factory=dict)

    # -- completeness helpers -----------------------------------------

    def resolution_supported(self) -> bool:
        return "relation_resolution" in self.schema_features

    # ``completeness_level`` values that mean files are actually missing from the
    # graph — not merely that some relations are weak or some languages are
    # inventory-only ("degraded", which is normal for any polyglot repo).
    _INCOMPLETE_LEVELS = frozenset({"partial", "incomplete", "failed", "error"})

    def snapshot_partial(self) -> bool:
        """True when the snapshot is missing files it should have parsed.

        ``partial_failures`` (a file the provider could not parse) or an
        explicit incomplete ``completeness_level``. A plain ``degraded`` level
        does *not* count — that is handled per-file via inventory-only languages
        and ``unparsed_files`` scoped to the change.
        """
        return bool(self.partial_failures) or (
            (self.completeness_level or "").lower() in self._INCOMPLETE_LEVELS
        )

    def inventory_only_languages(self) -> list[str]:
        return sorted(
            lang
            for lang, tier in self.language_tiers.items()
            if tier == evidence.INVENTORY_ONLY_TIER
        )

    def analysis_completeness(self) -> dict:
        """Machine-readable summary of what the graph could *not* fully resolve."""
        level = "ok"
        if self.snapshot_partial():
            level = "partial"
        elif (
            self.inventory_only_languages()
            or self.unparsed_files
            or (self.completeness_level or "").lower() == "degraded"
        ):
            level = "degraded"
        return {
            "level": level,
            "completeness_level": self.completeness_level,
            "partial_failures": [
                {k: v for k, v in pf.items() if k in ("path", "file", "code", "reason")}
                for pf in self.partial_failures
            ],
            "inventory_only_languages": self.inventory_only_languages(),
            "unparsed_files": sorted(self.unparsed_files),
        }

    # -- lookups --------------------------------------------------------

    def path_by_id(self) -> dict[str, str]:
        return {fid: node.path for fid, node in self.files.items()}

    def id_by_path(self) -> dict[str, str]:
        return {node.path: fid for fid, node in self.files.items()}

    def forward_reachable_paths(self, start: str) -> set[str]:
        """Every file ``start`` reaches by following forward ``IMPORTS`` edges."""
        id_by_path = self.id_by_path()
        start_id = id_by_path.get(start)
        if start_id is None:
            return set()
        by_id = self.path_by_id()
        seen: set[str] = set()
        stack = [start_id]
        while stack:
            current = stack.pop()
            node = self.files.get(current)
            if node is None:
                continue
            for dep_id in node.imports:
                if dep_id in seen:
                    continue
                seen.add(dep_id)
                stack.append(dep_id)
        return {by_id[i] for i in seen if i in by_id}

    def imported_by_paths(self) -> dict[str, set[str]]:
        """``path -> {paths that import it}`` (internal file→file only)."""
        by_id = self.path_by_id()
        out: dict[str, set[str]] = {node.path: set() for node in self.files.values()}
        for node in self.files.values():
            for importer_id in node.imported_by:
                importer_path = by_id.get(importer_id)
                if importer_path is not None:
                    out.setdefault(node.path, set()).add(importer_path)
        return out

    def imported_by_edges(self) -> dict[str, dict[str, str]]:
        """``path -> {importer_path: evidence class}`` for internal IMPORTS.

        Same shape as :meth:`imported_by_paths` but each importer carries the
        class of the edge that connects it, so a blast-radius walk can keep the
        weakest link it crossed.
        """
        by_id = self.path_by_id()
        out: dict[str, dict[str, str]] = {
            node.path: {} for node in self.files.values()
        }
        for node in self.files.values():
            for importer_id in node.imported_by:
                importer_path = by_id.get(importer_id)
                if importer_path is None:
                    continue
                cls = self.edge_class.get(
                    (importer_path, node.path), evidence.HEURISTIC
                )
                out.setdefault(node.path, {})[importer_path] = cls
        return out

    def as_dict(self) -> dict:
        by_id = self.path_by_id()
        files = {}
        for fid, node in self.files.items():
            files[fid] = {
                "path": node.path,
                "language": node.language,
                "imports": sorted(by_id.get(i, i) for i in node.imports),
                "imported_by": sorted(by_id.get(i, i) for i in node.imported_by),
            }
        return {
            "files": files,
            "relations": list(self.relations),
            "repo_root": self.repo_root,
            "repo_key": self.repo_key,
            "commit": self.commit,
        }


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------

def resolve_path_from_id(record_id: str, files_map: dict[str, str] | None = None) -> str | None:
    """Best-effort repo-relative path for a snapshot record id.

    File ids look like ``<repo_key>:file:<path>``; symbol ids look like
    ``<repo_key>:<language>:<path>:<kind>:<name>``. ``repo_key`` never contains
    a colon. External targets (``external:import:x``) have no path.
    """
    if not record_id:
        return None
    if files_map is not None and record_id in files_map:
        return files_map[record_id]
    if record_id.startswith("external:"):
        return None
    if _FILE_MARKER in record_id:
        return record_id.split(_FILE_MARKER, 1)[1] or None
    parts = record_id.split(":")
    if len(parts) >= 4:
        # <repo_key>:<language>:<path>:<kind>[:<name>...]
        return parts[2] or None
    return None


def _is_internal(record_id: str) -> bool:
    return bool(record_id) and not record_id.startswith("external:") and _FILE_MARKER in record_id


# ---------------------------------------------------------------------------
# Snapshot parsing
# ---------------------------------------------------------------------------

def _run_snapshot(repo_root: str, *, worktree: bool, entire_bin: str) -> str:
    binary = shutil.which(entire_bin) or entire_bin
    cmd = [binary, "graph", "snapshot", "--repo", repo_root, "--format", "ndjson"]
    if worktree:
        cmd.append("--worktree")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SNAPSHOT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise SnapshotError(
            f"`{entire_bin}` not found on PATH; install the Entire CLI + graph plugin"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SnapshotError("`entire graph snapshot` timed out") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-5:]
        raise SnapshotError(
            "`entire graph snapshot` failed (exit "
            f"{proc.returncode}): {' / '.join(tail)}"
        )
    return proc.stdout


def parse_snapshot_ndjson(source: str | Iterable[str]) -> Graph:
    """Parse NDJSON text (or an iterable of lines) into a :class:`Graph`."""
    if isinstance(source, str):
        lines: Iterable[str] = source.splitlines()
    else:
        lines = source

    graph = Graph()
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue

        record_type = obj.get("record_type")

        if record_type is None and "schema_version" in obj:
            graph.repo_root = obj.get("repo_root")
            graph.repo_key = obj.get("repo_key")
            graph.commit = obj.get("commit")
            graph.schema_features = list(obj.get("schema_features") or [])
            continue

        if record_type == "file":
            fid = obj.get("id")
            path = obj.get("path")
            if not fid or not path:
                continue
            graph.files[fid] = FileNode(id=fid, path=path, language=obj.get("language"))
            continue

        if record_type == "relation" and obj.get("type") == "IMPORTS":
            from_id = obj.get("from_id")
            to_id = obj.get("to_id")
            if not _is_internal(from_id) or not _is_internal(to_id):
                continue  # skip external:import:* and symbol-scoped edges
            # Keep the provider's grading fields; the evidence class is computed
            # in _finalize() once the summary record has been seen.
            graph.relations.append(
                {
                    "from_id": from_id,
                    "to_id": to_id,
                    "type": "IMPORTS",
                    "confidence": obj.get("confidence"),
                    "resolution": obj.get("resolution"),
                    "relation_scope": obj.get("relation_scope"),
                    "target_kind": obj.get("target_kind"),
                    "warning_codes": list(obj.get("warning_codes") or []),
                }
            )
            continue

        if record_type == "relation" and obj.get("type") == "TESTS":
            # symbol -> symbol; resolve both sides to file paths. The test-side
            # path is also carried in evidence[].file_path as a fallback.
            edge = _resolve_tests_edge(obj)
            if edge is not None:
                graph.test_edges.append(edge)
            continue

        if record_type == "summary":
            graph.partial_failures = list(obj.get("partial_failures") or [])
            graph.warnings = list(obj.get("warnings") or [])
            graph.language_tiers = dict(obj.get("language_tiers") or {})
            stats = obj.get("stats") or {}
            graph.completeness_level = stats.get("completeness_level") or "ok"
            continue

        # symbol / external / anything else: ignored.

    _link_relations(graph)
    _finalize_evidence(graph)
    return graph


def _finalize_evidence(graph: Graph) -> None:
    """Second pass: tier the files, resolve unparsed paths, grade every edge."""
    for node in graph.files.values():
        node.language_tier = graph.language_tiers.get(node.language or "")

    files_map = graph.path_by_id()
    id_by_path = graph.id_by_path()
    unparsed: set[str] = set()
    for pf in graph.partial_failures:
        raw = pf.get("path") or pf.get("file") or pf.get("file_path")
        if not raw:
            continue
        resolved = resolve_path_from_id(raw, files_map)
        if not resolved and ":" not in raw:
            resolved = raw  # provider gave a bare repo-relative path
        if not resolved:
            continue
        unparsed.add(resolved)
        node = graph.files.get(id_by_path.get(resolved, ""))
        if node is not None:
            node.parsed = False
    graph.unparsed_files = sorted(unparsed)

    # Grade each edge on its own merits. A snapshot-wide parse failure elsewhere
    # in the repo (a broken example file, say) must NOT poison unrelated edges —
    # only an edge that actually touches an unparsed file is downgraded here, and
    # surface-scoped partial-ness is applied later in resolve_affected_tests().
    unparsed_set = set(graph.unparsed_files)
    supported = graph.resolution_supported()
    for rel in graph.relations:
        src_path = resolve_path_from_id(rel["from_id"], files_map)
        dst_path = resolve_path_from_id(rel["to_id"], files_map)
        src_node = graph.files.get(rel["from_id"])
        tier = src_node.language_tier if src_node is not None else None
        if (src_path in unparsed_set) or (dst_path in unparsed_set):
            cls = evidence.UNVERIFIED
        else:
            cls = evidence.classify_relation(
                rel,
                language_tier=tier,
                resolution_supported=supported,
            )
        rel["evidence_class"] = cls
        if src_path and dst_path:
            graph.edge_class[(src_path, dst_path)] = cls


def _resolve_tests_edge(obj: dict) -> dict | None:
    from_id = obj.get("from_id") or ""
    to_id = obj.get("to_id") or ""
    test_file = resolve_path_from_id(from_id)
    if test_file is None:
        for ev in obj.get("evidence") or []:
            if isinstance(ev, dict) and ev.get("file_path"):
                test_file = ev["file_path"]
                break
    source_file = resolve_path_from_id(to_id)
    if not test_file or not source_file:
        return None
    source_symbol = to_id.split(":")[-1] if ":" in to_id else None
    return {
        "test_file": test_file,
        "source_file": source_file,
        "source_symbol": source_symbol,
    }


def _link_relations(graph: Graph) -> None:
    for rel in graph.relations:
        src = graph.files.get(rel["from_id"])
        dst = graph.files.get(rel["to_id"])
        if src is None or dst is None:
            continue
        src.imports.add(dst.id)
        dst.imported_by.add(src.id)


def parse_entire_graph_snapshot(
    repo_root: str = ".",
    *,
    worktree: bool = True,
    entire_bin: str = DEFAULT_ENTIRE_BIN,
    snapshot_text: str | None = None,
) -> dict:
    """Run ``entire graph snapshot`` and return the parsed graph as a dict.

    Shape (SPEC.md "parser.py")::

        {files: {id: {path, language, imports, imported_by}}, relations: [...]}

    Pass ``snapshot_text`` to parse pre-captured NDJSON instead of shelling out.
    """
    return build_graph(
        repo_root,
        worktree=worktree,
        entire_bin=entire_bin,
        snapshot_text=snapshot_text,
    ).as_dict()


def build_graph(
    repo_root: str = ".",
    *,
    worktree: bool = True,
    entire_bin: str = DEFAULT_ENTIRE_BIN,
    snapshot_text: str | None = None,
) -> Graph:
    """Like :func:`parse_entire_graph_snapshot` but returns the :class:`Graph`."""
    if snapshot_text is None:
        snapshot_text = _run_snapshot(repo_root, worktree=worktree, entire_bin=entire_bin)
    return parse_snapshot_ndjson(snapshot_text)


# ---------------------------------------------------------------------------
# git diff
# ---------------------------------------------------------------------------

def _git(repo_root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo_root, *args],
        capture_output=True,
        text=True,
    )


def get_modified_files(repo_root: str = ".") -> list[str]:
    """Repo-relative paths that git reports as changed.

    Union of unstaged changes, staged changes, and untracked ``.py`` files
    (SPEC.md: "``git diff --name-only`` (staged + unstaged). Falls back to
    ``git ls-files --others`` for untracked .py files").
    """
    found: set[str] = set()

    for extra in (("diff", "--name-only"), ("diff", "--name-only", "--cached")):
        proc = _git(repo_root, *extra)
        if proc.returncode == 0:
            found.update(p for p in proc.stdout.splitlines() if p.strip())

    others = _git(repo_root, "ls-files", "--others", "--exclude-standard")
    if others.returncode == 0:
        found.update(
            p for p in others.stdout.splitlines() if p.strip().endswith(".py")
        )

    return sorted(found)


# ---------------------------------------------------------------------------
# Reverse-IMPORTS blast radius
# ---------------------------------------------------------------------------

def reverse_import_surface(
    modified: Sequence[str],
    imported_by: dict[str, set[str]],
    *,
    max_depth: int = 0,
) -> list[str]:
    """BFS out from ``modified`` along reverse ``IMPORTS`` edges.

    ``max_depth`` 0 means unlimited; 1 means direct importers only; N means N
    hops. Modified files are always part of the surface.
    """
    seen: set[str] = set()
    queue: deque[tuple[str, int]] = deque()
    for path in modified:
        if path not in seen:
            seen.add(path)
            queue.append((path, 0))

    while queue:
        path, depth = queue.popleft()
        if max_depth and depth >= max_depth:
            continue
        for importer in sorted(imported_by.get(path, ())):
            if importer not in seen:
                seen.add(importer)
                queue.append((importer, depth + 1))

    return sorted(seen)


def reverse_import_surface_classified(
    modified: Sequence[str],
    imported_by_edges: dict[str, dict[str, str]],
    *,
    max_depth: int = 0,
) -> dict[str, str]:
    """Like :func:`reverse_import_surface` but returns ``{path: evidence class}``.

    Each entry carries the *weakest* edge class crossed on the shortest chain
    from a modified file to that entry — the "graph is evidence, not an oracle"
    rule: a surface file reachable only through a ``heuristic`` import is itself
    ``heuristic``; one reached through an ``unverified`` edge is ``unverified``.
    Modified files themselves are ``confirmed`` (git, not the graph, put them
    there).
    """
    best: dict[str, str] = {}
    queue: deque[tuple[str, int]] = deque()
    for path in modified:
        if path not in best:
            best[path] = evidence.CONFIRMED
            queue.append((path, 0))

    while queue:
        path, depth = queue.popleft()
        if max_depth and depth >= max_depth:
            continue
        here = best.get(path, evidence.CONFIRMED)
        for importer, edge_cls in sorted(imported_by_edges.get(path, {}).items()):
            reached = evidence.weakest((here, edge_cls))
            if importer not in best or evidence.is_weaker(reached, best[importer]):
                best[importer] = reached
                queue.append((importer, depth + 1))

    return best


def get_modified_import_surface(
    repo_root: str = ".",
    *,
    modified_files: Sequence[str] | None = None,
    graph: Graph | None = None,
    max_depth: int = 0,
    worktree: bool = True,
    entire_bin: str = DEFAULT_ENTIRE_BIN,
    snapshot_text: str | None = None,
) -> list[str]:
    """Flat list of every file affected by the current modifications."""
    if graph is None:
        graph = build_graph(
            repo_root,
            worktree=worktree,
            entire_bin=entire_bin,
            snapshot_text=snapshot_text,
        )
    if modified_files is None:
        modified_files = get_modified_files(repo_root)

    surface = reverse_import_surface(
        list(modified_files), graph.imported_by_paths(), max_depth=max_depth
    )
    # Guarantee modified files appear even if the graph did not index them.
    return sorted(set(surface) | set(modified_files))


def get_modified_import_surface_classified(
    repo_root: str = ".",
    *,
    modified_files: Sequence[str] | None = None,
    graph: Graph | None = None,
    max_depth: int = 0,
    worktree: bool = True,
    entire_bin: str = DEFAULT_ENTIRE_BIN,
    snapshot_text: str | None = None,
) -> dict[str, str]:
    """``{path: evidence class}`` for the whole blast radius of the current edits.

    A modified file the graph never indexed is still returned, classed
    ``unverified`` — the graph had nothing to say about it.
    """
    if graph is None:
        graph = build_graph(
            repo_root,
            worktree=worktree,
            entire_bin=entire_bin,
            snapshot_text=snapshot_text,
        )
    if modified_files is None:
        modified_files = get_modified_files(repo_root)

    classified = reverse_import_surface_classified(
        list(modified_files), graph.imported_by_edges(), max_depth=max_depth
    )
    indexed = set(graph.path_by_id().values())
    for path in modified_files:
        if path not in indexed:
            classified[path] = evidence.UNVERIFIED
        elif path not in classified:
            classified[path] = evidence.CONFIRMED
    return classified


# ---------------------------------------------------------------------------
# Dashboard projection
# ---------------------------------------------------------------------------

def get_graph_for_dashboard(
    repo_root: str = ".",
    *,
    graph: Graph | None = None,
    modified_files: Sequence[str] | None = None,
    import_surface: Sequence[str] | None = None,
    worktree: bool = True,
    entire_bin: str = DEFAULT_ENTIRE_BIN,
    snapshot_text: str | None = None,
) -> dict:
    """``{nodes: [...], edges: [...]}`` for React Flow.

    A node carries ``id`` (its path), ``label`` (basename), ``path``,
    ``language``, and ``modified`` / ``in_surface`` state flags. Only files
    that participate in an internal ``IMPORTS`` edge, or are modified, are
    included — isolated files would just be noise on the canvas.
    """
    if graph is None:
        graph = build_graph(
            repo_root,
            worktree=worktree,
            entire_bin=entire_bin,
            snapshot_text=snapshot_text,
        )
    if modified_files is None:
        modified_files = get_modified_files(repo_root)
    modified_set = set(modified_files)
    surface_set = set(import_surface) if import_surface is not None else set()

    by_id = graph.path_by_id()
    edges = []
    connected: set[str] = set()
    for rel in graph.relations:
        src = by_id.get(rel["from_id"])
        dst = by_id.get(rel["to_id"])
        if src is None or dst is None:
            continue
        edges.append({"id": f"{src}->{dst}", "source": src, "target": dst})
        connected.add(src)
        connected.add(dst)

    node_paths = connected | modified_set
    nodes = []
    lang_by_path = {node.path: node.language for node in graph.files.values()}
    for path in sorted(node_paths):
        nodes.append(
            {
                "id": path,
                "label": os.path.basename(path),
                "path": path,
                "language": lang_by_path.get(path),
                "modified": path in modified_set,
                "in_surface": path in surface_set,
            }
        )

    # Drop edges that point at a node we are not rendering.
    edges = [e for e in edges if e["source"] in node_paths and e["target"] in node_paths]
    return {"nodes": nodes, "edges": edges}
