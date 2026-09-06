from src.config import DEFAULT_ALGORITHM, load_config


def test_defaults():
    config = load_config()
    assert config["algorithm"] == DEFAULT_ALGORITHM
    assert config["ttl_seconds"] == 3600


def test_overrides_win():
    config = load_config({"ttl_seconds": 60})
    assert config["ttl_seconds"] == 60
