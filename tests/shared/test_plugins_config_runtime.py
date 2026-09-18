"""plugins_config.load_for_runtime — dangling config entries degrade, never raise.

QA nit 2 from PR #878: `load()` stayed fail-fast at every consumer other than
`_load_extensions`. `load_for_runtime` is the shared runtime wrapper those
consumers use; strict `load()` (interactive CLI paths) keeps raising. Dangling
names route through the one canonical reporter (`shared.plugin_load_report`,
once per process) — the 2026-09-11 macmini incident ran for days on a plain
warning no alert surface carried.
"""

from pathlib import Path

import pytest

from shared import paths, plugin_load_report, plugins_config
from shared.config import settings
from shared.plugins_config import DanglingPlugin, load, load_for_runtime, write_local


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    user = tmp_path / "plugins"
    user.mkdir()
    monkeypatch.setattr(paths, "plugins_dir", lambda: user)
    monkeypatch.setattr(paths, "plugins_config_path", lambda: tmp_path / "plugins.json")
    monkeypatch.setattr(settings.general, "ava_home", str(tmp_path / "ava"))
    monkeypatch.setattr(paths, "ava_home", lambda: tmp_path)
    # The once-per-process report memo is interpreter-global module state;
    # reset it so one test's report cannot suppress another test's assertion.
    monkeypatch.setattr(plugins_config, "_dangling_reported", set[str]())


def test_load_for_runtime_drops_dangling_and_reports_each_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_local(
        {
            "plugins": {
                "real": {"enabled": True},
                "zulu": {"enabled": False},
                "alpha": {"enabled": True},
            }
        }
    )
    reported: list[tuple[str, BaseException]] = []

    def _capture(name: str, exc: BaseException) -> None:
        reported.append((name, exc))

    monkeypatch.setattr(plugin_load_report, "report_plugin_load_failure", _capture)

    config = load_for_runtime({"real"})  # must not raise

    assert set(config.plugins) == {"real"}
    assert config.plugins["real"].enabled
    assert [name for name, _ in reported] == ["alpha", "zulu"]
    assert isinstance(reported[0][1], DanglingPlugin)


def test_load_for_runtime_reports_each_name_once_per_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapper sits on gateway request paths; a repeat load in the same
    process must not re-report — the first report is the signal."""
    write_local({"plugins": {"real": {"enabled": True}, "vanished": {"enabled": False}}})
    reported: list[str] = []

    def _capture(name: str, exc: BaseException) -> None:
        reported.append(name)

    monkeypatch.setattr(plugin_load_report, "report_plugin_load_failure", _capture)

    load_for_runtime({"real"})
    load_for_runtime({"real"})  # a later request re-reads the same config

    assert reported == ["vanished"]


def test_load_for_runtime_keeps_enabled_flags() -> None:
    write_local({"plugins": {"real": {"enabled": False}}})

    config = load_for_runtime({"real"})

    assert config.plugins["real"].enabled is False


def test_strict_load_still_raises_on_dangling() -> None:
    """Interactive CLI paths keep fail-fast: `ava plugins enable` on a plugin
    that is not on disk must keep its DanglingPlugin error."""
    write_local({"plugins": {"vanished": {"enabled": True}}})

    with pytest.raises(DanglingPlugin):
        load({"real"})
