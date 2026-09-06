"""Toy JWT helpers for the Git-Blast demo repo. Not real crypto."""

import base64
import hashlib
import hmac
import json

from .config import load_config


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def encode(payload: dict, overrides: dict | None = None) -> str:
    config = load_config(overrides)
    header = {"alg": config["algorithm"], "typ": "JWT"}
    segments = [
        _b64(json.dumps(header, separators=(",", ":")).encode()),
        _b64(json.dumps(payload, separators=(",", ":")).encode()),
    ]
    signing_input = ".".join(segments).encode()
    signature = hmac.new(
        config["secret"].encode(), signing_input, hashlib.sha256
    ).digest()
    segments.append(_b64(signature))
    return ".".join(segments)


def verify(token: str, overrides: dict | None = None) -> bool:
    config = load_config(overrides)
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError:
        return False
    signing_input = f"{header_b64}.{payload_b64}".encode()
    expected = hmac.new(
        config["secret"].encode(), signing_input, hashlib.sha256
    ).digest()
    return _b64(expected) == signature_b64
