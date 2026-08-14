"""scripts/release_cut.py: pure cadence logic (bump, tag parse, catchup plan).

The git/gh-touching paths (_cut, _day_target, _latest_dated) are exercised by
real cuts, not here.
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import patch

import pytest

from scripts.release_cut import _SEED, _TAG, _bump, _catchup_days, _merged_prs


def test_bump_seeds_when_no_prior_tag() -> None:
    assert _bump("daily", None) == _SEED
    assert _bump("weekly", None) == _SEED


def test_bump_daily_patch_weekly_minor() -> None:
    assert _bump("daily", (0, 8, 6)) == (0, 8, 7)
    assert _bump("weekly", (0, 8, 6)) == (0, 9, 0)


@pytest.mark.parametrize(
    ("tag", "version", "stamp"),
    [
        ("v0.8.0-20260601", (0, 8, 0), "20260601"),
        ("v1.12.3-202606101830", (1, 12, 3), "20260610"),  # HHMM suffix variant
    ],
)
def test_tag_regex_parses_version_and_date(
    tag: str, version: tuple[int, int, int], stamp: str
) -> None:
    m = _TAG.match(tag)
    assert m is not None
    assert (int(m[1]), int(m[2]), int(m[3])) == version
    assert m[4] == stamp


@pytest.mark.parametrize("tag", ["v0.7-thin-client", "v0.8.0", "v0.8.0-202606", "0.8.0-20260601"])
def test_tag_regex_rejects_non_dated_tags(tag: str) -> None:
    assert _TAG.match(tag) is None


def test_catchup_days_covers_gap_and_classifies_mondays() -> None:
    # v0.8.0 cut Mon 2026-06-01; catchup run Wed 2026-06-10.
    days = _catchup_days(date(2026, 6, 1), date(2026, 6, 10))
    assert [d.day for d, _ in days] == list(range(2, 11))
    assert dict(days)[date(2026, 6, 8)] == "weekly"  # the Monday
    assert all(m == "daily" for d, m in days if d != date(2026, 6, 8))


def test_catchup_days_empty_when_already_current() -> None:
    assert _catchup_days(date(2026, 6, 10), date(2026, 6, 10)) == []


def _fake_run(shas_out: str, gh_out: str, subjects_out: str):
    def run(cmd: list[str]) -> str:
        if cmd[0] == "gh":
            return gh_out
        if "--format=%H" in cmd:
            return shas_out
        return subjects_out

    return run


def test_merged_prs_matches_gh_pr_by_mergecommit_sha_no_pr_suffix() -> None:
    # Rebase-merge: commit subjects carry no trailing "(#N)", so only the
    # gh-side mergeCommit.oid intersection can find these.
    gh_out = json.dumps(
        [
            {
                "number": 42,
                "title": "feat: rebase-merged pr",
                "mergedAt": "2026-07-20T10:00:00Z",
                "mergeCommit": {"oid": "aaa111"},
            },
            {
                "number": 7,
                "title": "fix: from a prior release, not in range",
                "mergedAt": "2026-07-01T10:00:00Z",
                "mergeCommit": {"oid": "zzz999"},
            },
        ]
    )
    subjects = (
        "aaa111\x002026-07-20T10:00:00+00:00\x00feat: rebase-merged pr\n"
        "bbb222\x002026-07-20T11:00:00+00:00\x00chore: no pr number\n"
    )
    with patch("scripts.release_cut._run", _fake_run("aaa111\nbbb222\n", gh_out, subjects)):
        prs = _merged_prs("v0.10.48-20260719", "origin/main")
    assert [p["number"] for p in prs] == [42]
    assert prs[0]["title"] == "feat: rebase-merged pr"


def test_merged_prs_falls_back_to_subject_suffix_when_not_in_gh_result() -> None:
    # A PR gh's search window/state filter missed, but the commit subject
    # still carries the legacy "(#N)" suffix (squash/merge-commit history).
    shas = "ccc333\n"
    gh_out = json.dumps([])
    subjects = "ccc333\x002026-07-20T12:00:00+00:00\x00fix: squash merged (#99)\n"
    with patch("scripts.release_cut._run", _fake_run(shas, gh_out, subjects)):
        prs = _merged_prs("v0.10.48-20260719", "origin/main")
    assert [p["number"] for p in prs] == [99]


def test_merged_prs_empty_range_short_circuits_without_calling_gh() -> None:
    with patch("scripts.release_cut._run", _fake_run("", "should not be reached", "")):
        assert _merged_prs("v0.10.48-20260719", "origin/main") == []


# ── HHMM (same-day multi-cut) tests ──


def test_tag_regex_captures_hhmm_when_present() -> None:
    m = _TAG.match("v0.11.4-202607232230")
    assert m is not None
    assert (int(m[1]), int(m[2]), int(m[3])) == (0, 11, 4)
    assert m[4] == "20260723"
    assert m[5] == "2230"


def test_tag_regex_hhmm_is_none_when_absent() -> None:
    m = _TAG.match("v0.11.4-20260723")
    assert m is not None
    assert m[5] is None


def _fake_git_tags(*tags: str):
    """Return a _run that produces the given tag list from `git tag --list`."""

    def run(cmd: list[str]) -> str:
        if cmd == ["git", "tag", "--list"]:
            return "\n".join(tags)
        raise AssertionError(f"unexpected call: {cmd}")

    return run


def test_latest_dated_returns_hhmm() -> None:
    from scripts.release_cut import _latest_dated

    with patch(
        "scripts.release_cut._run",
        _fake_git_tags(
            "v0.11.4-20260723",
            "v0.11.4-202607232230",
        ),
    ):
        result = _latest_dated()
        assert result is not None
        tag, ver, day, hhmm = result
    assert tag == "v0.11.4-202607232230"  # HHMM tag wins over no-HHMM on same day
    assert ver == (0, 11, 4)
    assert day == date(2026, 7, 23)
    assert hhmm == "2230"


def test_latest_dated_prefers_later_hhmm_same_version() -> None:
    from scripts.release_cut import _latest_dated

    with patch(
        "scripts.release_cut._run",
        _fake_git_tags(
            "v0.11.4-202607232230",
            "v0.11.4-202607232330",
        ),
    ):
        result = _latest_dated()
        assert result is not None
        tag, ver, _day, hhmm = result
    assert tag == "v0.11.4-202607232330"  # later HHMM wins
    assert ver == (0, 11, 4)
    assert hhmm == "2330"


def test_latest_dated_higher_version_wins_regardless_of_hhmm() -> None:
    from scripts.release_cut import _latest_dated

    with patch(
        "scripts.release_cut._run",
        _fake_git_tags(
            "v0.11.4-202607232230",
            "v0.11.5-20260724",  # higher patch, no HHMM — wins
        ),
    ):
        result = _latest_dated()
        assert result is not None
        tag, ver, _day, hhmm = result
    assert tag == "v0.11.5-20260724"
    assert ver == (0, 11, 5)
    assert hhmm is None


def test_latest_dated_returns_none_when_no_dated_tags() -> None:
    from scripts.release_cut import _latest_dated

    with patch("scripts.release_cut._run", _fake_git_tags("v0.7-thin-client")):
        assert _latest_dated() is None


def test_latest_dated_hhmm_vs_no_hhmm_same_version() -> None:
    # Tag without HHMM comes first on a day; tag with HHMM is later
    from scripts.release_cut import _latest_dated

    with patch(
        "scripts.release_cut._run",
        _fake_git_tags(
            "v0.11.4-202607232230",
            "v0.11.4-20260723",  # no HHMM — earlier on the same day
        ),
    ):
        result = _latest_dated()
        assert result is not None
        tag, _ver, _day, hhmm = result
    assert tag == "v0.11.4-202607232230"  # HHMM wins over no-HHMM
    assert hhmm == "2230"


# ── post-refactor locks: planning / finishing split out of main() ──


def _latest(day="20260719", hhmm=None, version=(0, 10, 48)):
    return (
        f"v{version[0]}.{version[1]}.{version[2]}-{day}{hhmm or ''}",
        version,
        date(int(day[:4]), int(day[4:6]), int(day[6:8])),
        hhmm,
    )


def test_plan_daily_same_day_keeps_patch_appends_hhmm() -> None:
    from datetime import UTC, datetime

    from scripts.release_cut import _plan_daily

    latest = _latest(day="20260723", hhmm="1200", version=(0, 11, 4))
    now = datetime(2026, 7, 23, 12, 30, tzinfo=UTC)
    version, hhmm, prev_tag = _plan_daily(latest, now, date(2026, 7, 23))
    assert version == (0, 11, 4)  # patch kept, not bumped
    assert hhmm == "1230"
    assert prev_tag == "v0.11.4-202607231200"


def test_plan_daily_rejects_same_minute_collision() -> None:
    from datetime import UTC, datetime

    import pytest

    from scripts.release_cut import _plan_daily

    latest = _latest(day="20260723", hhmm="1230")
    now = datetime(2026, 7, 23, 12, 30, tzinfo=UTC)
    with pytest.raises(SystemExit, match="already cut; wait one minute"):
        _plan_daily(latest, now, date(2026, 7, 23))


def test_plan_daily_new_day_bumps_patch_no_hhmm() -> None:
    from datetime import UTC, datetime

    from scripts.release_cut import _plan_daily

    latest = _latest(day="20260722", hhmm=None, version=(0, 11, 4))
    now = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
    version, hhmm, prev_tag = _plan_daily(latest, now, date(2026, 7, 23))
    assert version == (0, 11, 5)
    assert hhmm is None
    assert prev_tag == "v0.11.4-20260722"


def test_plan_daily_seeds_when_no_latest() -> None:
    from datetime import UTC, datetime

    from scripts.release_cut import _SEED, _plan_daily

    now = datetime(2026, 7, 23, 9, 0, tzinfo=UTC)
    version, hhmm, prev_tag = _plan_daily(None, now, date(2026, 7, 23))
    assert version == _SEED
    assert hhmm is None
    assert prev_tag is None


def test_cut_daily_passes_plan_to_cut() -> None:
    from datetime import UTC, datetime

    from scripts.release_cut import _cut_daily

    with patch("scripts.release_cut._cut", return_value="v0.11.5-20260723") as cut:
        created = _cut_daily(
            _latest(day="20260722", version=(0, 11, 4)),
            datetime(2026, 7, 23, 9, 0, tzinfo=UTC),
            date(2026, 7, 23),
        )
    cut.assert_called_once_with(
        "daily", date(2026, 7, 23), "HEAD", (0, 11, 5), prev_tag="v0.11.4-20260722", hhmm=None
    )
    assert created == ["v0.11.5-20260723"]


def test_cut_weekly_passes_plan_to_cut() -> None:
    from scripts.release_cut import _cut_weekly

    with patch("scripts.release_cut._cut", return_value=None) as cut:
        created = _cut_weekly(_latest(day="20260722", version=(0, 11, 4)), date(2026, 7, 27))
    cut.assert_called_once_with(
        "weekly", date(2026, 7, 27), "HEAD", (0, 12, 0), prev_tag="v0.11.4-20260722"
    )
    assert created == []  # idle window -> no tag


def test_cut_catchup_requires_existing_tag() -> None:
    import pytest

    from scripts.release_cut import _cut_catchup

    with pytest.raises(SystemExit, match="seed with daily first"):
        _cut_catchup(None, date(2026, 6, 10))


def test_cut_catchup_advances_version_only_on_cut() -> None:
    from scripts.release_cut import _cut_catchup

    # Mon 2026-06-01 tagged; catchup over Jun 2-3. Jun 2 daily bumps 0.8.0->0.8.1;
    # Jun 3 daily bumps to 0.8.2. Mock _cut so the middle day cuts nothing.
    latest = _latest(day="20260601", hhmm=None, version=(0, 8, 0))
    calls: list[tuple[object, ...]] = []

    def fake_cut(
        mode: object,
        day: date,
        target: object,
        version: tuple[int, int, int],
        prev_tag: object | None = None,
        hhmm: object | None = None,
    ) -> str | None:
        calls.append((mode, day, version, prev_tag))
        return (
            None
            if day == date(2026, 6, 3)
            else f"v{version[0]}.{version[1]}.{version[2]}-{day.strftime('%Y%m%d')}"
        )

    with (
        patch("scripts.release_cut._cut", side_effect=fake_cut),
        patch("scripts.release_cut._day_target", side_effect=lambda d: f"sha-{d:%Y%m%d}"),
    ):
        created = _cut_catchup(latest, date(2026, 6, 3))
    # daily(Jun2, v0.8.1, prev v0.8.0) -> cut; daily(Jun3, v0.8.2, prev v0.8.1-20260602) -> idle
    assert calls == [
        ("daily", date(2026, 6, 2), (0, 8, 1), "v0.8.0-20260601"),
        ("daily", date(2026, 6, 3), (0, 8, 2), "v0.8.1-20260602"),
    ]
    assert created == ["v0.8.1-20260602"]


def test_finish_nothing_to_cut() -> None:
    from scripts.release_cut import _finish

    with patch("scripts.release_cut._run") as run:
        _finish([], push=True, release=True)
    run.assert_not_called()


def test_finish_push_and_release() -> None:
    from unittest.mock import call

    from scripts.release_cut import _finish

    with patch("scripts.release_cut._run") as run:
        _finish(["v0.8.1-20260602", "v0.8.2-20260603"], push=True, release=True)
    assert run.call_args_list == [
        call(["git", "push", "origin", "v0.8.1-20260602", "v0.8.2-20260603"]),
        call(
            [
                "gh",
                "release",
                "create",
                "v0.8.1-20260602",
                "--title",
                "v0.8.1-20260602",
                "--notes-from-tag",
            ]
        ),
        call(
            [
                "gh",
                "release",
                "create",
                "v0.8.2-20260603",
                "--title",
                "v0.8.2-20260603",
                "--notes-from-tag",
            ]
        ),
    ]


def test_finish_local_only_does_not_push() -> None:
    from scripts.release_cut import _finish

    with patch("scripts.release_cut._run") as run:
        _finish(["v0.8.1-20260602"], push=False, release=False)
    run.assert_not_called()


def test_parse_args_release_implies_push() -> None:
    from scripts.release_cut import _parse_args

    with patch("sys.argv", ["release_cut", "daily", "--release"]):
        args = _parse_args()
    assert args.mode == "daily"
    assert args.push is True
    assert args.release is True


def test_gh_pr_command_bootstrap_window() -> None:
    from scripts.release_cut import _gh_pr_command

    cmd = _gh_pr_command(None)
    assert cmd[:9] == [
        "gh",
        "pr",
        "list",
        "--state",
        "merged",
        "--base",
        "main",
        "--json",
        "number,title,mergedAt,mergeCommit",
    ]
    assert cmd[9:] == ["--limit", "1000"]


def test_gh_pr_command_search_window_since_prev_tag() -> None:
    from scripts.release_cut import _gh_pr_command

    cmd = _gh_pr_command("v0.10.48-20260719")
    assert cmd[9:] == ["--limit", "500", "--search", "merged:>=2026-07-18"]


def test_gh_pr_command_rejects_non_dated_prev_tag() -> None:
    import pytest

    from scripts.release_cut import _gh_pr_command

    with pytest.raises(SystemExit, match="not a dated release tag"):
        _gh_pr_command("v0.7-thin-client")


def test_merge_commit_prs_keeps_only_range_shas() -> None:
    import json

    from scripts.release_cut import _merge_commit_prs

    gh_out = json.dumps(
        [
            {"number": 1, "title": "a", "mergedAt": "t1", "mergeCommit": {"oid": "aaa"}},
            {"number": 2, "title": "b", "mergedAt": "t2", "mergeCommit": {"oid": "zzz"}},
        ]
    )
    prs = _merge_commit_prs(gh_out, {"aaa"})
    assert list(prs) == [1]
    assert prs[1] == {"number": 1, "title": "a", "mergedAt": "t1"}


def test_subject_suffix_prs_unions_missing_prs() -> None:
    from scripts.release_cut import _subject_suffix_prs

    log_out = (
        "aaa\x002026-07-20T10:00:00+00:00\x00feat: already known (#1)\n"
        "bbb\x002026-07-20T11:00:00+00:00\x00fix: newly found (#99)\n"
    )
    prs = {1: {"number": 1, "title": "already known", "mergedAt": "x"}}
    _subject_suffix_prs(log_out, prs)
    assert list(prs) == [1, 99]
    assert prs[99]["title"] == "fix: newly found (#99)"


# --- staging gate (#1190): dated tags require a green staging deploy ---


class _FakeCompleted:
    """Minimal CompletedProcess stand-in for _git_quiet tests."""

    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


def test_latest_staging_sha_uses_local_ref_without_fetch() -> None:
    from scripts.release_cut import _latest_staging_sha

    with patch("scripts.release_cut._git_quiet", return_value=_FakeCompleted(0, "abc123")) as gq:
        assert _latest_staging_sha() == "abc123"
    gq.assert_called_once_with("rev-parse", "-q", "--verify", "refs/tags/latest")


def test_latest_staging_sha_fetches_once_when_local_ref_missing() -> None:
    from scripts.release_cut import _latest_staging_sha

    seen: list[str] = []

    def fake(cmd: str, *args: str) -> _FakeCompleted:
        seen.append(cmd)
        if cmd == "fetch":
            return _FakeCompleted(0)
        # first rev-parse misses the local ref; the one after fetch succeeds
        if seen.count("rev-parse") == 2:
            return _FakeCompleted(0, "def456")
        return _FakeCompleted(1)

    with patch("scripts.release_cut._git_quiet", side_effect=fake) as gq:
        assert _latest_staging_sha() == "def456"
    assert [c.args[0] for c in gq.call_args_list] == ["rev-parse", "fetch", "rev-parse"]


def test_latest_staging_sha_none_when_absent_even_after_fetch() -> None:
    from scripts.release_cut import _latest_staging_sha

    with patch("scripts.release_cut._git_quiet", return_value=_FakeCompleted(1)):
        assert _latest_staging_sha() is None


def test_staging_gate_ok_when_latest_equals_target() -> None:
    from scripts.release_cut import _staging_gate

    with patch("scripts.release_cut._latest_staging_sha", return_value="deadbeef"):
        _staging_gate("deadbeef")  # must not raise


def test_staging_gate_rejects_missing_latest() -> None:
    import pytest

    from scripts.release_cut import _staging_gate

    with (
        patch("scripts.release_cut._latest_staging_sha", return_value=None),
        pytest.raises(SystemExit, match="does not exist"),
    ):
        _staging_gate("deadbeef")


def test_staging_gate_rejects_unverified_target() -> None:
    import pytest

    from scripts.release_cut import _staging_gate

    with (
        patch("scripts.release_cut._latest_staging_sha", return_value="feedc0de"),
        pytest.raises(SystemExit, match="has not passed the staging gate"),
    ):
        _staging_gate("deadbeef")


def test_main_daily_applies_gate_to_head() -> None:
    from scripts.release_cut import main

    with (
        patch("scripts.release_cut._latest_staging_sha", return_value="deadbeef"),
        patch(
            "scripts.release_cut._run",
            side_effect=lambda cmd: "deadbeef\n" if cmd[:2] == ["git", "rev-parse"] else "",
        ),
        patch("scripts.release_cut._cut_daily", return_value=[]) as cut,
        patch("sys.argv", ["release_cut", "daily"]),
    ):
        main()
    cut.assert_called_once()  # gate passed -> the cut ran


def test_main_catchup_requires_latest_exists() -> None:
    import pytest

    from scripts.release_cut import main

    with (
        patch("scripts.release_cut._latest_staging_sha", return_value=None),
        patch("scripts.release_cut._cut_catchup") as cut,
        patch("sys.argv", ["release_cut", "catchup"]),
        pytest.raises(SystemExit, match="cannot cut a stable channel"),
    ):
        main()
    cut.assert_not_called()
