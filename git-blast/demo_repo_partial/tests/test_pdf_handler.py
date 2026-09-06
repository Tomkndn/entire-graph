"""Exercises src/handlers/pdf.py — but only through the runtime registry, so the
code graph cannot connect this test to that module.
"""

from src import core


def test_pdf_render_roundtrip():
    out = core.render("pdf", {"title": "Q3", "pages": 12})
    assert out.startswith("%PDF-1.4 Q3")


def test_pdf_unknown_format_rejected():
    try:
        core.render("xml", {})
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown format")
