from pathlib import Path

import pytest

from tests.fixtures import provisioning


@pytest.mark.parametrize(
    ("root_parts", "args", "env", "cwd", "blocked"),
    [
        pytest.param(("Meiva",), ["tests"], {}, None, True, id="bare-run-testpaths-injected"),
        pytest.param(("Meiva",), ["tests"], {}, None, True, id="explicit-tests-directory"),
        pytest.param(("Meiva",), ["tests/components/meiva"], {}, None, True, id="directory-only"),
        pytest.param(("Meiva",), ["tests/components/meiva/test_x.py"], {}, None, False, id="file"),
        pytest.param(
            ("Meiva",),
            ["tests/components/meiva/test_x.py::test_y"],
            {},
            None,
            False,
            id="file-nodeid",
        ),
        pytest.param(("Meiva",), ["tests"], {}, None, True, id="keyword-filter-without-file"),
        pytest.param(
            ("Meiva",),
            ["--ignore=tests/components/meiva/test_x.py", "tests"],
            {},
            None,
            True,
            id="file-looking-option",
        ),
        pytest.param(
            ("Meiva",),
            ["--ignore", "tests/components/meiva/test_x.py"],
            {},
            None,
            True,
            id="ignore-space-value-only",
        ),
        pytest.param(
            ("Meiva",),
            ["--deselect", "tests/components/meiva/test_x.py::test_y"],
            {},
            None,
            True,
            id="deselect-nodeid-value",
        ),
        pytest.param(
            ("Meiva",),
            ["--ignore", "tests/components/meiva/test_x.py", "tests/components/meiva/test_x.py"],
            {},
            None,
            False,
            id="ignore-value-plus-real-file",
        ),
        pytest.param(
            ("Meiva",), ["tests"], {"GITHUB_ACTIONS": "true"}, None, False, id="github-ci"
        ),
        pytest.param(
            ("Meiva",),
            ["tests"],
            {"MEIVA_ALLOW_FULL_PYTEST": "1"},
            None,
            False,
            id="approved-override",
        ),
        pytest.param(
            ("Meiva", ".worktrees", "feature"),
            ["tests"],
            {},
            None,
            False,
            id="worktree",
        ),
        pytest.param((".meiva", "source"), ["tests"], {}, None, True, id="production-checkout"),
        pytest.param(
            ("Meiva",),
            ["components/meiva/test_x.py"],
            {},
            ("Meiva", "tests"),
            False,
            id="in-tests-dir-explicit-path",
        ),
        pytest.param(
            ("Meiva",),
            ["components/meiva/test_x.py"],
            {},
            ("Meiva",),
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
    selected_file = rootpath / "tests/components/meiva/test_x.py"
    selected_file.parent.mkdir(parents=True)
    selected_file.touch()

    message = provisioning.full_run_guard_message(rootpath, args, env, invocation_dir)

    assert (message is not None) is blocked
