"""Minimal config module for the Git-Blast demo repo."""

DEFAULT_TTL_SECONDS = 3600
DEFAULT_ALGORITHM = "HS256"


def load_config(overrides: dict | None = None) -> dict:
    config = {
        "ttl_seconds": DEFAULT_TTL_SECONDS,
        "algorithm": DEFAULT_ALGORITHM,
        "secret": "demo-secret",
    }
    if overrides:
        config.update(overrides)
    return config
