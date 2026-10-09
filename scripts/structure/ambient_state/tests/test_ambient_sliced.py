"""The settings-read rule of the ambient-state gate (scripts/structure/ambient_state/sliced.py):
a slice-governed package reads no process-global `settings` outside its composition root."""

from __future__ import annotations

import ast
import pathlib
import textwrap

import pytest

from scripts.structure import ambient_state
from scripts.structure.ambient_state import allowlist as allow

_PACKAGE = "services/entrypoints/im_bridge"
_ROOT = f"{_PACKAGE}/daemon.py"


def _sites(source: str, rel: str) -> dict[str, int]:
    tree = ast.parse(textwrap.dedent(source))
    measured = ambient_state.measure(tree, rel, pathlib.Path("/nonexistent"))
    return {key.split("::", 1)[1]: len(lines) for key, lines in measured.items()}


@pytest.fixture(autouse=True)
def _governed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared structure-test isolation blanks the real list; govern one package here."""
    monkeypatch.setattr(allow, "SLICED_PACKAGES", {_PACKAGE: frozenset({_ROOT})})


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from base.config import settings\n", {"settings-read:settings": 1}),
        ("from base.config import settings as cfg\n", {"settings-read:settings": 1}),
        ("from base.config import get_field\n", {"settings-read:get_field": 1}),
        ("from base.config._lite import get_field\n", {"settings-read:get_field": 1}),
        ("import base.config\n", {"settings-read:base.config": 1}),
        ("from base import config\n", {"settings-read:base.config": 1}),
        (
            "def f():\n    from base.config import settings\n    return settings.x.y\n",
            {"settings-read:settings": 1},
        ),
        (
            "from base.config import settings\n\ndef f():\n    from base.config import settings\n",
            {"settings-read:settings": 2},
        ),
    ],
)
def test_a_module_of_a_sliced_package_may_not_import_the_global_settings(
    source: str, expected: dict[str, int]
) -> None:
    assert _sites(source, f"{_PACKAGE}/core.py") == expected


def test_the_composition_root_may_read_settings() -> None:
    assert _sites("from base.config import settings\n", _ROOT) == {}


@pytest.mark.parametrize(
    "rel",
    [
        "services/wake/heartbeat/core.py",  # not governed
        f"{_PACKAGE}/tests/test_core.py",  # tests are outside the rule
        f"{_PACKAGE}/adapters/tests/test_feishu.py",
    ],
)
def test_other_modules_are_outside_the_rule(rel: str) -> None:
    assert _sites("from base.config import settings\n", rel) == {}


def test_other_names_of_base_config_are_not_reads() -> None:
    assert _sites("from base.config import cluster_tz, Settings\n", f"{_PACKAGE}/core.py") == {}


def test_a_listed_package_or_root_that_is_gone_is_stale(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/config.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(allow, "SLICED_PACKAGES", {"pkg": frozenset({"pkg/root.py"})})
    errors = [e for e in ambient_state.missing_allowlist_errors(tmp_path) if "SLICED" in e]
    assert len(errors) == 1
    assert "pkg/root.py does not exist" in errors[0]
