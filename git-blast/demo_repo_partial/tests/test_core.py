"""Statically reachable: imports src.core, which the graph resolves normally."""

from src import core


def test_supported_formats():
    assert core.supported_formats() == ["csv", "pdf"]


def test_render_csv():
    out = core.render("csv", {"b": 2, "a": 1})
    assert out.splitlines()[0] == "a,b"
