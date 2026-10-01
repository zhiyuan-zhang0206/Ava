"""services.browser.orphan — naming this cluster's Chrome without walking to it.

The weight of this file is on the REFUSALS. A post-handoff orphan that survives
costs the operator one manual kill; a Chrome wrongly selected costs them their
logged-in browser, on a class of machine where the operator's own Chrome is
always running. So every shape of not-ours is asserted directly against the
predicate with fabricated process records — a Chrome on a different profile, a
Chrome with no `--user-data-dir` at all (the operator's daily browser, measured
on the dev Mac as a flagless argv), a Chrome whose argv cannot be read, and a
non-Chrome process that merely mentions the path — before the positive case and
the no-orphan no-op.
"""

from pathlib import Path

import pytest

from services.browser import orphan

_PROFILE = Path("/home/ava/.ava-wt/chrome-profile")


def _chrome(*extra: str, udd: Path | str | None = _PROFILE) -> list[str]:
    """An argv shaped like the daemon's own (`_chrome_args`), plus `extra`."""
    argv = ["/usr/bin/google-chrome", "--remote-debugging-port=9222"]
    if udd is not None:
        argv.append(f"--user-data-dir={udd}")
    return [*argv, "--no-first-run", *extra]


# --- refusals: a Chrome that is not ours must never be selected -------------


def test_a_chrome_on_another_profile_is_not_ours() -> None:
    """The operator's explicitly-profiled Chrome (or another cluster's) carries a
    different `--user-data-dir`, which is the whole basis of the identification."""
    other = _chrome(udd="/Users/op/.cache/chrome-devtools-mcp/chrome-profile")
    assert orphan.is_cluster_chrome("Google Chrome", other, _PROFILE) is False


def test_a_sibling_clusters_profile_is_not_ours() -> None:
    """Profiles are per-cluster under `$AVA_HOME`; a prefix relationship is not a
    match, so the prod cluster's Chrome survives a worktree cluster's teardown."""
    sibling = _chrome(udd="/home/ava/.ava/chrome-profile")
    assert orphan.is_cluster_chrome("chrome", sibling, _PROFILE) is False
    nested = _chrome(udd=str(_PROFILE) + "-old")
    assert orphan.is_cluster_chrome("chrome", nested, _PROFILE) is False


def test_a_chrome_with_no_user_data_dir_is_not_ours() -> None:
    """The operator's daily Chrome runs on the platform default and passes no
    `--user-data-dir` at all — the single most important process to never kill."""
    daily = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    assert orphan.is_cluster_chrome("Google Chrome", daily, _PROFILE) is False
    assert orphan.is_cluster_chrome("Google Chrome", _chrome(udd=None), _PROFILE) is False


def test_an_unreadable_cmdline_is_skipped_not_an_error() -> None:
    """Another user's process: argv unreadable (`AccessDenied`) arrives as None.
    Unidentified is not a licence to kill, and it is not a failure either — the
    teardown carries on."""
    assert orphan.is_cluster_chrome("chrome", None, _PROFILE) is False


def test_an_empty_cmdline_is_not_ours() -> None:
    """Zombies / kernel threads report an empty argv."""
    assert orphan.is_cluster_chrome("chrome", [], _PROFILE) is False


def test_a_non_chrome_process_mentioning_the_profile_is_not_ours() -> None:
    """A `grep` or an editor whose command line happens to contain the path passes
    the token test; the executable-name test is what keeps it out of the kill."""
    grepping = ["grep", "-r", f"--user-data-dir={_PROFILE}", "."]
    assert orphan.is_cluster_chrome("grep", grepping, _PROFILE) is False


def test_chromes_own_helper_processes_are_not_selected() -> None:
    """Renderer / GPU / utility children inherit the profile token but carry
    `--type=`. They are reaped as descendants of the browser process, so selecting
    them directly would only produce a noisy pid list."""
    for kind in ("renderer", "gpu-process", "utility", "zygote"):
        helper = _chrome(f"--type={kind}")
        assert orphan.is_cluster_chrome("Google Chrome Helper", helper, _PROFILE) is False


# --- the positive case -----------------------------------------------------


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
    """`--user-data-dir <path>` as two argv entries — the shape a Chrome that
    relaunched itself can re-tokenize its command line into. It is still ours and
    must still identify (the win 2026-08-11 mystery holder class)."""
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
    """Compared as paths, not strings — a normalising difference must not make the
    orphan invisible."""
    assert orphan.is_cluster_chrome("chrome", _chrome(udd=f"{_PROFILE}/"), _PROFILE) is True


# --- find / reap over a fabricated process table ---------------------------


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
    """Install `procs` as the process table and record what gets tree-killed."""
    monkeypatch.setattr(orphan.psutil, "process_iter", lambda: iter(procs))
    monkeypatch.setattr(orphan, "profile_dir", lambda: _PROFILE)
    killed: list[int] = []
    monkeypatch.setattr(orphan, "kill_process_tree", killed.append)
    return killed


def test_find_picks_only_ours_out_of_a_crowded_table(monkeypatch: pytest.MonkeyPatch) -> None:
    procs = [
        _FakeProc(101, "Google Chrome", ["/Applications/Google Chrome"]),  # operator's daily
        _FakeProc(102, "Google Chrome", _chrome(udd="/Users/op/other/profile")),  # other profile
        _FakeProc(103, "Google Chrome Helper", _chrome("--type=renderer")),  # our helper
        _FakeProc(104, "chrome", None),  # unreadable
        _FakeProc(105, "grep", ["grep", f"--user-data-dir={_PROFILE}"]),  # not a browser
        _FakeProc(106, "Google Chrome", _chrome()),  # ← ours
    ]
    _table(monkeypatch, procs)
    assert orphan.find_cluster_chrome() == [106]


def test_reap_kills_the_orphans_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    killed = _table(monkeypatch, [_FakeProc(106, "chrome.exe", _chrome())])
    assert orphan.reap_cluster_chrome() == [106]
    assert killed == [106], "the named root goes to kill_process_tree, helpers follow as children"


def test_reap_with_no_orphan_is_a_silent_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The overwhelmingly common case: nothing matched, nothing killed, no output."""
    procs = [
        _FakeProc(101, "Google Chrome", ["/Applications/Google Chrome"]),
        _FakeProc(102, "python", ["python", "-m", "cli.main", "stop"]),
    ]
    killed = _table(monkeypatch, procs)
    assert orphan.reap_cluster_chrome() == []
    assert killed == []


def test_reap_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second sweep finds nothing — and `kill_process_tree` is itself a no-op on
    a pid already gone, so re-running a teardown is safe."""
    procs = [_FakeProc(106, "chrome", _chrome())]
    killed = _table(monkeypatch, procs)
    assert orphan.reap_cluster_chrome() == [106]
    monkeypatch.setattr(orphan.psutil, "process_iter", lambda: iter([]))
    assert orphan.reap_cluster_chrome() == []
    assert killed == [106]
