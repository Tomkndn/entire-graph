"""Public entry point. Statically imports the registry and the generated schema;
reaches the format handlers only through ``registry.dispatch`` at runtime.
"""

from __future__ import annotations

from generated import _pb2
from src import registry


def supported_formats() -> list[str]:
    return sorted(_pb2.SCHEMA["formats"])


def render(fmt: str, payload: dict) -> str:
    if fmt not in _pb2.SCHEMA["formats"]:
        raise ValueError(f"unknown format: {fmt}")
    return registry.dispatch(fmt, payload)
