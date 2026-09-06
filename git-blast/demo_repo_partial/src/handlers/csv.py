"""CSV handler. Also reached only through the runtime registry."""

from __future__ import annotations


def render(payload: dict) -> str:
    keys = sorted(payload)
    header = ",".join(keys)
    row = ",".join(str(payload[k]) for k in keys)
    return f"{header}\n{row}"
