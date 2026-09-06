"""PDF handler. Reached only via ``registry.dispatch("pdf", ...)`` — no static
importer, so the code graph shows this file with zero dependents.
"""

from __future__ import annotations


def render(payload: dict) -> str:
    title = payload.get("title", "untitled")
    return f"%PDF-1.4 {title} ({len(payload)} fields)"
