"""Stage 3 coverage: convention-based src -> test matching."""

from src.matcher import (
    in_test_tree,
    looks_like_test_file,
    match_surface,
    match_tests,
    source_stem,
)


def test_looks_like_test_file_is_strict():
    assert looks_like_test_file("tests/test_auth.py")
    assert looks_like_test_file("pkg/auth_test.py")
    # helpers and conftest live in the test tree but are not runnable modules
    assert not looks_like_test_file("a/tests/helpers.py")
    assert not looks_like_test_file("tests/utils.py")
    assert not looks_like_test_file("tests/conftest.py")
    assert not looks_like_test_file("src/auth.py")
    assert not looks_like_test_file("tests/test_auth.txt")
    assert not looks_like_test_file("README.md")


def test_in_test_tree():
    assert in_test_tree("tests/test_auth.py")
    assert in_test_tree("tests/utils.py")
    assert in_test_tree("tests/conftest.py")
    assert in_test_tree("a/b/test/fixtures.py")
    assert not in_test_tree("src/auth.py")
    assert not in_test_tree("src/testing.py")  # "test" must be a path segment
    assert not in_test_tree("tests/data.json")


def test_source_stem():
    assert source_stem("src/requests/auth.py") == "auth"
    assert source_stem("auth.py") == "auth"
    assert source_stem("src/requests/__init__.py") == "requests"


def test_exact_basename_anywhere():
    tests = ["tests/test_auth.py", "tests/test_other.py", "pkg/auth_test.py"]
    assert match_tests("src/requests/auth.py", tests) == [
        "tests/test_auth.py",
        "pkg/auth_test.py",
    ]


def test_sibling_test_dir():
    tests = ["src/requests/tests/test_auth.py"]
    assert match_tests("src/requests/auth.py", tests) == [
        "src/requests/tests/test_auth.py"
    ]


def test_mirrored_path_src_swapped_for_tests():
    tests = ["tests/requests/test_auth.py"]
    assert match_tests("src/requests/auth.py", tests) == [
        "tests/requests/test_auth.py"
    ]


def test_package_init_matches_via_dir_name():
    tests = ["tests/test_requests.py"]
    assert match_tests("src/requests/__init__.py", tests) == ["tests/test_requests.py"]


def test_token_prefix_loose_match_is_last():
    tests = ["tests/test_auth.py", "tests/test_auth_helpers.py"]
    result = match_tests("src/auth.py", tests)
    assert result[0] == "tests/test_auth.py"
    assert "tests/test_auth_helpers.py" in result


def test_no_match_returns_empty():
    assert match_tests("src/auth.py", ["tests/test_billing.py"]) == []


def test_source_that_is_itself_a_test_returns_empty():
    assert match_tests("tests/test_auth.py", ["tests/test_auth.py"]) == []


def test_results_are_deduplicated():
    tests = ["tests/test_auth.py"]
    # exact + mirrored strategies could both point here
    assert match_tests("src/auth.py", tests) == ["tests/test_auth.py"]


def test_match_surface_unions_over_sources():
    tests = ["tests/test_auth.py", "tests/test_config.py"]
    surface = ["src/auth.py", "src/config.py", "tests/test_auth.py"]
    assert sorted(match_surface(surface, tests)) == [
        "tests/test_auth.py",
        "tests/test_config.py",
    ]
