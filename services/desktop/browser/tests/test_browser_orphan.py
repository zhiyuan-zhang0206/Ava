from pathlib import Path

import pytest

from services.desktop.browser import orphan

_PROFILE = Path("/home/ava/.ava-wt/chrome-profile")


def _chrome(*extra: str, udd: Path | str | None = _PROFILE) -> list[str]:
    argv = ["/usr/bin/google-chrome", "--remote-debugging-port=9222"]
    if udd is not None:
        argv.append(f"--user-data-dir={udd}")
    return [*argv, "--no-first-run", *extra]


def test_a_chrome_on_another_profile_is_not_ours() -> None:
    other = _chrome(udd="/Users/op/.cache/chrome-devtools-mcp/chrome-profile")
    assert orphan.is_cluster_chrome("Google Chrome", other, _PROFILE) is False


def test_a_sibling_clusters_profile_is_not_ours() -> None:
    sibling = _chrome(udd="/home/ava/.ava/chrome-profile")
    assert orphan.is_cluster_chrome("chrome", sibling, _PROFILE) is False
    nested = _chrome(udd=str(_PROFILE) + "-old")
    assert orphan.is_cluster_chrome("chrome", nested, _PROFILE) is False


def test_a_chrome_with_no_user_data_dir_is_not_ours() -> None:
    daily = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    assert orphan.is_cluster_chrome("Google Chrome", daily, _PROFILE) is False
    assert orphan.is_cluster_chrome("Google Chrome", _chrome(udd=None), _PROFILE) is False


def test_an_unreadable_cmdline_is_skipped_not_an_error() -> None:
    assert orphan.is_cluster_chrome("chrome", None, _PROFILE) is False


def test_an_empty_cmdline_is_not_ours() -> None:
    assert orphan.is_cluster_chrome("chrome", [], _PROFILE) is False


def test_a_non_chrome_process_mentioning_the_profile_is_not_ours() -> None:
    grepping = ["grep", "-r", f"--user-data-dir={_PROFILE}", "."]
    assert orphan.is_cluster_chrome("grep", grepping, _PROFILE) is False


def test_chromes_own_helper_processes_are_not_selected() -> None:
    for kind in ("renderer", "gpu-process", "utility", "zygote"):
        helper = _chrome(f"--type={kind}")
        assert orphan.is_cluster_chrome("Google Chrome Helper", helper, _PROFILE) is False


def test_our_browser_process_is_selected() -> None:
    argv = _chrome()
    assert orphan.is_cluster_chrome("chrome", argv, _PROFILE) is True


@pytest.mark.parametrize(
    "name",
    ["chrome.exe", "Google Chrome", "chrome", "google-chrome-stable", "chromium-browser"],
)
def test_every_platforms_browser_executable_name_is_recognised(name: str) -> None:
    assert orphan.is_cluster_chrome(name, _chrome(), _PROFILE) is True


def test_the_split_token_form_is_recognised() -> None:
    split = [
        "chrome.exe",
        "--remote-debugging-port=9222",
        "--user-data-dir",
        str(_PROFILE),
        "--no-first-run",
    ]
    assert orphan.is_cluster_chrome("chrome.exe", split, _PROFILE) is True


def test_the_split_token_form_on_another_profile_is_still_not_ours() -> None:
    split = ["chrome.exe", "--remote-debugging-port=9222", "--user-data-dir", "/elsewhere"]
    assert orphan.is_cluster_chrome("chrome.exe", split, _PROFILE) is False


def test_a_trailing_separator_still_matches() -> None:
    assert orphan.is_cluster_chrome("chrome", _chrome(udd=f"{_PROFILE}/"), _PROFILE) is True


class _FakeProc:
    def __init__(self, pid: int, name: str, cmdline: list[str] | None) -> None:
        self.pid = pid
        self._name = name
        self._cmdline = cmdline

    def name(self) -> str:
        if self._cmdline is None:
            raise orphan.psutil.AccessDenied(self.pid)
        return self._name

    def cmdline(self) -> list[str]:
        if self._cmdline is None:
            raise orphan.psutil.AccessDenied(self.pid)
        return self._cmdline


def _table(monkeypatch: pytest.MonkeyPatch, procs: list[_FakeProc]) -> list[int]:
    monkeypatch.setattr(orphan.psutil, "process_iter", lambda: iter(procs))
    monkeypatch.setattr(orphan, "profile_dir", lambda: _PROFILE)
    killed: list[int] = []
    monkeypatch.setattr(orphan, "kill_process_tree", killed.append)
    return killed


def test_find_picks_only_ours_out_of_a_crowded_table(monkeypatch: pytest.MonkeyPatch) -> None:
    procs = [
        _FakeProc(101, "Google Chrome", ["/Applications/Google Chrome"]),
        _FakeProc(102, "Google Chrome", _chrome(udd="/Users/op/other/profile")),
        _FakeProc(103, "Google Chrome Helper", _chrome("--type=renderer")),
        _FakeProc(104, "chrome", None),
        _FakeProc(105, "grep", ["grep", f"--user-data-dir={_PROFILE}"]),
        _FakeProc(106, "Google Chrome", _chrome()),
    ]
    _table(monkeypatch, procs)
    assert orphan.find_cluster_chrome() == [106]


def test_reap_kills_the_orphans_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    killed = _table(monkeypatch, [_FakeProc(106, "chrome.exe", _chrome())])
    assert orphan.reap_cluster_chrome() == [106]
    assert killed == [106], "the named root goes to kill_process_tree, helpers follow as children"


def test_reap_with_no_orphan_is_a_silent_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    procs = [
        _FakeProc(101, "Google Chrome", ["/Applications/Google Chrome"]),
        _FakeProc(102, "python", ["python", "-m", "cli.main", "stop"]),
    ]
    killed = _table(monkeypatch, procs)
    assert orphan.reap_cluster_chrome() == []
    assert killed == []


def test_reap_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    procs = [_FakeProc(106, "chrome", _chrome())]
    killed = _table(monkeypatch, procs)
    assert orphan.reap_cluster_chrome() == [106]
    monkeypatch.setattr(orphan.psutil, "process_iter", lambda: iter([]))
    assert orphan.reap_cluster_chrome() == []
    assert killed == [106]


_RACE_SYSTEM_ERROR = "<built-in function proc_cmdline> returned a result with an exception set"
_RACE_PERMISSION_ERROR = (
    "[Errno 13] force permission denied (originated from sysctl(KERN_PROCARGS2) -> errno 0)"
)


class _RacingProc(_FakeProc):
    def __init__(self, pid: int, exc: Exception) -> None:
        super().__init__(pid, "", [])
        self._exc = exc

    def cmdline(self) -> list[str]:
        raise self._exc


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(SystemError(_RACE_SYSTEM_ERROR), id="system-error"),
        pytest.param(PermissionError(_RACE_PERMISSION_ERROR), id="permission-error"),
    ],
)
def test_a_process_the_scan_cannot_read_is_skipped_not_fatal(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    procs = [_RacingProc(999, exc), _FakeProc(106, "Google Chrome", _chrome())]
    _table(monkeypatch, procs)
    assert orphan.find_cluster_chrome() == [106]


def test_reap_survives_a_process_the_scan_cannot_read(monkeypatch: pytest.MonkeyPatch) -> None:
    procs = [
        _RacingProc(999, SystemError(_RACE_SYSTEM_ERROR)),
        _FakeProc(106, "chrome", _chrome()),
    ]
    killed = _table(monkeypatch, procs)
    assert orphan.reap_cluster_chrome() == [106]
    assert killed == [106]
