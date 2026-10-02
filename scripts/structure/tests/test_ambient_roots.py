"""The composition roots a package declares in its own `ambient_roots.toml`."""

from __future__ import annotations

import pathlib

import pytest

from scripts.structure.ambient_state.roots import ROOTS_FILE, package_roots


def _declare(root: pathlib.Path, package: str, text: str) -> None:
    (root / package).mkdir(parents=True, exist_ok=True)
    (root / package / ROOTS_FILE).write_text(text, encoding="utf-8")


def test_a_package_declares_its_roots_relative_to_itself(tmp_path: pathlib.Path) -> None:
    _declare(tmp_path, "services/a", 'settings = ["daemon.py", "boot/root.py"]\n')
    _declare(tmp_path, "services/b", 'settings = ["main.py"]\n')

    assert package_roots(tmp_path, "settings") == {
        "services/a": frozenset({"services/a/daemon.py", "services/a/boot/root.py"}),
        "services/b": frozenset({"services/b/main.py"}),
    }


def test_declarations_in_hidden_or_vendored_directories_are_not_read(
    tmp_path: pathlib.Path,
) -> None:
    for skipped in (".worktrees/x", ".venv/lib", "ui/web/node_modules/p"):
        _declare(tmp_path, skipped, 'settings = ["daemon.py"]\n')

    assert package_roots(tmp_path, "settings") == {}


@pytest.mark.parametrize(
    "text",
    ['database = ["daemon.py"]\n', 'settings = "daemon.py"\n', "settings = []\n"],
)
def test_a_malformed_declaration_is_refused(tmp_path: pathlib.Path, text: str) -> None:
    _declare(tmp_path, "services/a", text)

    with pytest.raises(ValueError, match=r"services/a/ambient_roots\.toml"):
        package_roots(tmp_path, "settings")


def test_a_handle_package_may_declare_no_root(tmp_path: pathlib.Path) -> None:
    """A package that only takes the bus builds none; its `bus = []` still governs it."""
    _declare(tmp_path, "gateway/events", "bus = []\n")

    assert package_roots(tmp_path, "bus") == {"gateway/events": frozenset()}


def test_an_unknown_kind_is_refused(tmp_path: pathlib.Path) -> None:
    with pytest.raises(ValueError, match="unknown composition-root kind"):
        package_roots(tmp_path, "database")
