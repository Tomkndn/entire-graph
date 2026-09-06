from src.jwt import encode, verify


def test_roundtrip():
    token = encode({"sub": "demo"})
    assert verify(token) is True


def test_tampered_token_fails():
    token = encode({"sub": "demo"})
    assert verify(token + "x") is False


def test_wrong_secret_fails():
    token = encode({"sub": "demo"})
    assert verify(token, {"secret": "other"}) is False
