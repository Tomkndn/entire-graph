"""Runtime handler lookup — the reason this repo is only partially analysable.

``dispatch()`` turns a format name into a module path and imports it with
``importlib.import_module``, then pulls the ``render`` callable off it with
``getattr``. No static ``import src.handlers.pdf`` statement exists anywhere, so
the code graph has no edge from here (or from ``core``) to the handler modules.
"""

from __future__ import annotations

import importlib

_HANDLER_PACKAGE = "src.handlers"


def load_handler(fmt: str):
    """Import ``src.handlers.<fmt>`` and return its ``render`` function."""
    module = importlib.import_module(f"{_HANDLER_PACKAGE}.{fmt}")
    return getattr(module, "render")


def dispatch(fmt: str, payload: dict) -> str:
    handler = load_handler(fmt)
    return handler(payload)
