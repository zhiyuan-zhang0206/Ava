"""Decision-table tests for the shared-checkout full-pytest guard."""

from pathlib import Path

import pytest

from tests import conftest


@pytest.mark.parametrize(
    ("root_parts", "args", "env", "cwd", "blocked"),
    [
        pytest.param(("Ava",), ["tests"], {}, None, True, id="bare-run-testpaths-injected"),
        pytest.param(("Ava",), ["tests"], {}, None, True, id="explicit-tests-directory"),
        pytest.param(("Ava",), ["tests/ava"], {}, None, True, id="directory-only"),
        pytest.param(("Ava",), ["tests/ava/test_x.py"], {}, None, False, id="file"),
        pytest.param(
            ("Ava",),
            ["tests/ava/test_x.py::test_y"],
            {},
            None,
            False,
            id="file-nodeid",
        ),
        pytest.param(("Ava",), ["tests"], {}, None, True, id="keyword-filter-without-file"),
        pytest.param(
            ("Ava",),
            ["--ignore=tests/ava/test_x.py", "tests"],
            {},
            None,
            True,
            id="file-looking-option",
        ),
        # Space-separated value of a value-taking option must not count as a
        # positional file arg (`pytest --ignore tests/x.py` collects the whole
        # suite while ignoring one path).
        pytest.param(
            ("Ava",),
            ["--ignore", "tests/ava/test_x.py"],
            {},
            None,
            True,
            id="ignore-space-value-only",
        ),
        pytest.param(
            ("Ava",),
            ["--deselect", "tests/ava/test_x.py::test_y"],
            {},
            None,
            True,
            id="deselect-nodeid-value",
        ),
        # ...but a real positional file alongside still counts as targeted.
        pytest.param(
            ("Ava",),
            ["--ignore", "tests/ava/test_x.py", "tests/ava/test_x.py"],
            {},
            None,
            False,
            id="ignore-value-plus-real-file",
        ),
        pytest.param(("Ava",), ["tests"], {"GITHUB_ACTIONS": "true"}, None, False, id="github-ci"),
        pytest.param(
            ("Ava",),
            ["tests"],
            {"AVA_ALLOW_FULL_PYTEST": "1"},
            None,
            False,
            id="approved-override",
        ),
        pytest.param(
            ("Ava", ".worktrees", "feature"),
            ["tests"],
            {},
            None,
            False,
            id="worktree",
        ),
        pytest.param((".ava", "source"), ["tests"], {}, None, True, id="production-checkout"),
        # Explicit relative file path given from inside tests/ — pytest resolves
        # positional args against the invocation dir, not the rootdir.
        pytest.param(
            ("Ava",),
            ["ava/test_x.py"],
            {},
            ("Ava", "tests"),
            False,
            id="in-tests-dir-explicit-path",
        ),
        # The same arg from the checkout root names no existing file — blocked
        # (pytest itself would also fail to find it there).
        pytest.param(
            ("Ava",),
            ["ava/test_x.py"],
            {},
            ("Ava",),
            True,
            id="relative-arg-missing-from-cwd",
        ),
    ],
)
def test_full_run_guard_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_parts: tuple[str, ...],
    args: list[str],
    env: dict[str, str],
    cwd: tuple[str, ...] | None,
    blocked: bool,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    rootpath = home.joinpath(*root_parts)
    invocation_dir = rootpath if cwd is None else home.joinpath(*cwd)
    selected_file = rootpath / "tests/ava/test_x.py"
    selected_file.parent.mkdir(parents=True)
    selected_file.touch()

    message = conftest._full_run_guard_message(rootpath, args, env, invocation_dir)

    assert (message is not None) is blocked
