from pathlib import Path

import pytest

from tests.fixtures import provisioning


@pytest.mark.parametrize(
    ("root_parts", "args", "env", "cwd", "blocked"),
    [
        pytest.param(("Ava",), ["tests"], {}, None, True, id="bare-run-testpaths-injected"),
        pytest.param(("Ava",), ["tests"], {}, None, True, id="explicit-tests-directory"),
        pytest.param(("Ava",), ["tests/components/ava"], {}, None, True, id="directory-only"),
        pytest.param(("Ava",), ["tests/components/ava/test_x.py"], {}, None, False, id="file"),
        pytest.param(
            ("Ava",),
            ["tests/components/ava/test_x.py::test_y"],
            {},
            None,
            False,
            id="file-nodeid",
        ),
        pytest.param(("Ava",), ["tests"], {}, None, True, id="keyword-filter-without-file"),
        pytest.param(
            ("Ava",),
            ["--ignore=tests/components/ava/test_x.py", "tests"],
            {},
            None,
            True,
            id="file-looking-option",
        ),
        pytest.param(
            ("Ava",),
            ["--ignore", "tests/components/ava/test_x.py"],
            {},
            None,
            True,
            id="ignore-space-value-only",
        ),
        pytest.param(
            ("Ava",),
            ["--deselect", "tests/components/ava/test_x.py::test_y"],
            {},
            None,
            True,
            id="deselect-nodeid-value",
        ),
        pytest.param(
            ("Ava",),
            ["--ignore", "tests/components/ava/test_x.py", "tests/components/ava/test_x.py"],
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
        pytest.param(
            ("Ava",),
            ["components/ava/test_x.py"],
            {},
            ("Ava", "tests"),
            False,
            id="in-tests-dir-explicit-path",
        ),
        pytest.param(
            ("Ava",),
            ["components/ava/test_x.py"],
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
    selected_file = rootpath / "tests/components/ava/test_x.py"
    selected_file.parent.mkdir(parents=True)
    selected_file.touch()

    message = provisioning.full_run_guard_message(rootpath, args, env, invocation_dir)

    assert (message is not None) is blocked
