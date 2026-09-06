# demo_repo

A two-module Python project used as the built-in target for Git-Blast Live
(SPEC.md "Tested Against"). `src/jwt.py` imports `src/config.py`, and each
module has a matching test under `tests/`.

Expected `module_test_map` after `seed --auto` (3 mappings):

| source_file | test_file |
|---|---|
| `src/config.py` | `tests/test_config.py` |
| `src/jwt.py` | `tests/test_jwt.py` |
| `src/config.py` | `tests/test_jwt.py` (transitive: `test_jwt` → `jwt` → `config`) |

Run its tests directly with `pytest` from this directory.
