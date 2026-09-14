"""PR-flow exporter — sampling, aggregation, cache, and failure-path contracts."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import types
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "pr_flow_export.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("pr_flow_export", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pr_flow = _load_script()
_TZ = ZoneInfo("Asia/Shanghai")


def _meta(
    number: int,
    *,
    created: str = "2026-09-12T02:00:00Z",
    merged: str = "2026-09-12T03:00:00Z",
    updated: str | None = None,
) -> dict[str, Any]:
    return {
        "number": number,
        "created_at": created,
        "updated_at": updated or merged,
        "merged_at": merged,
    }


def _receipt(
    sha: str, verdict: str = "approved", at: str = "2026-09-12T02:30:00Z"
) -> dict[str, Any]:
    body = (
        "```ava-qa\n"
        + json.dumps(
            {
                "ava_qa_version": 1,
                "pr_number": 1,
                "head_sha": sha,
                "verdict": verdict,
                "asserted_ava_reviewer": "Ava #3242",
            }
        )
        + "\n```"
    )
    return {"event": "commented", "created_at": at, "uid": 87293881, "body": body}


# ── window + percentile math ────────────────────────────────────────────────


def test_window_days_are_the_complete_days_before_today() -> None:
    now = datetime(2026, 9, 14, 0, 25, tzinfo=UTC)  # 08:25 CST
    days = pr_flow.window_days(now, 3, _TZ)
    assert days == [date(2026, 9, 11), date(2026, 9, 12), date(2026, 9, 13)]


def test_percentile_linear_interpolation() -> None:
    values = [10.0, 20.0, 30.0, 40.0]
    assert pr_flow._percentile(values, 0.5) == 25.0
    assert pr_flow._percentile(values, 0.9) == pytest.approx(37.0)
    assert pr_flow._percentile([7.0], 0.9) == 7.0


# ── receipts + head deltas ──────────────────────────────────────────────────


def test_parse_receipts_accepts_valid_skips_foreign_and_malformed() -> None:
    valid = _receipt("a" * 40)
    foreign = {**valid, "uid": 1}
    malformed = {**valid, "body": "not a receipt"}
    bad_json = {**valid, "body": "```ava-qa\n{oops\n```"}
    non_hex = {**valid, "body": valid["body"].replace("a" * 40, "z" * 40)}
    receipts = pr_flow.parse_receipts([valid, foreign, malformed, bad_json, non_hex])
    assert receipts == [
        {
            "at": "2026-09-12T02:30:00Z",
            "head_sha": "a" * 40,
            "verdict": "approved",
        }
    ]


def test_count_head_deltas_counts_receipts_followed_by_a_new_head() -> None:
    sha1, sha2 = "a" * 40, "b" * 40
    receipts = [
        {"at": "2026-09-12T02:00:00Z", "head_sha": sha1, "verdict": "approved"},
        {"at": "2026-09-12T04:00:00Z", "head_sha": sha2, "verdict": "approved"},
    ]
    events = [
        {"event": "committed", "commit_date": "2026-09-12T03:00:00Z", "sha": sha2},
    ]
    # Receipt #1 is followed by a new sha before receipt #2 -> one delta;
    # receipt #2 is followed by no commit before the merge -> none.
    assert pr_flow.count_head_deltas(receipts, events, "2026-09-12T05:00:00Z") == 1


def test_count_head_deltas_ignores_a_repushed_same_sha() -> None:
    sha = "a" * 40
    receipts = [{"at": "2026-09-12T02:00:00Z", "head_sha": sha, "verdict": "approved"}]
    events = [{"event": "committed", "commit_date": "2026-09-12T02:30:00Z", "sha": sha}]
    assert pr_flow.count_head_deltas(receipts, events, "2026-09-12T05:00:00Z") == 0


def test_count_head_deltas_flags_a_post_receipt_change_before_merge() -> None:
    sha1, sha2 = "a" * 40, "b" * 40
    receipts = [{"at": "2026-09-12T02:00:00Z", "head_sha": sha1, "verdict": "approved"}]
    events = [{"event": "committed", "commit_date": "2026-09-12T03:30:00Z", "sha": sha2}]
    assert pr_flow.count_head_deltas(receipts, events, "2026-09-12T05:00:00Z") == 1


def test_build_record_prefers_ready_for_review_over_created_at() -> None:
    events = [{"event": "ready_for_review", "created_at": "2026-09-12T02:10:00Z"}]
    record = pr_flow.build_record(_meta(1), events)
    assert record.ready_at == "2026-09-12T02:10:00Z"
    fallback = pr_flow.build_record(_meta(2), [])
    assert fallback.ready_at == "2026-09-12T02:00:00Z"


# ── daily aggregation ───────────────────────────────────────────────────────


def _record(number: int, *, merged: str, ready: str, receipts: int = 1, deltas: int = 0):
    return pr_flow.PrRecord(
        number=number,
        updated_at=merged,
        created_at=ready,
        merged_at=merged,
        ready_at=ready,
        receipts=[{"at": ready, "head_sha": "a" * 40, "verdict": "approved"}] * receipts,
        head_deltas=deltas,
    )


def test_compute_days_groups_by_merge_day_and_omits_empty_samples() -> None:
    records = [
        _record(1, merged="2026-09-12T02:00:00Z", ready="2026-09-12T01:00:00Z"),  # 3600s
        _record(2, merged="2026-09-12T03:00:00Z", ready="2026-09-12T01:00:00Z", deltas=1),  # 7200s
        _record(3, merged="2026-09-13T03:00:00Z", ready="2026-09-13T02:30:00Z"),  # 1800s
    ]
    days = [date(2026, 9, 11), date(2026, 9, 12), date(2026, 9, 13)]
    payload = pr_flow.compute_days(records, days, _TZ, flake_counts=None)
    d12 = payload["2026-09-12"]
    assert d12["merged_count"] == 2
    assert d12["ready_to_merge_median_seconds"] == pytest.approx(5400.0)
    assert d12["ready_to_merge_p90_seconds"] == pytest.approx(6840.0)
    assert d12["qa_rounds_mean"] == 1.0
    assert d12["qa_rereview_share"] == 0.5
    assert "flake_new_quarantines" not in d12  # unreachable flake source is not zero
    d11 = payload["2026-09-11"]
    assert d11 == {"merged_count": 0}
    assert "flake_new_quarantines" not in payload["2026-09-13"]
    with_flakes = pr_flow.compute_days(records, days, _TZ, flake_counts={date(2026, 9, 13): 3})
    assert with_flakes["2026-09-13"]["flake_new_quarantines"] == 3
    assert with_flakes["2026-09-12"]["flake_new_quarantines"] == 0


def test_duration_skips_non_positive_spans() -> None:
    record = _record(9, merged="2026-09-12T01:00:00Z", ready="2026-09-12T02:00:00Z")
    assert pr_flow._duration_seconds(record) is None


# ── cache-based record collection ───────────────────────────────────────────


def test_collect_records_reuses_cache_only_when_updated_at_is_unchanged() -> None:
    days = [date(2026, 9, 12)]
    meta = _meta(7)
    cached = {
        "7": {
            "updated_at": meta["updated_at"],
            "created_at": meta["created_at"],
            "merged_at": meta["merged_at"],
            "ready_at": meta["created_at"],
            "receipts": [],
            "head_deltas": 0,
            "partial": False,
        }
    }
    stats = pr_flow.RunStats()
    records = pr_flow.collect_records([meta], days, _TZ, "r/x", cached, stats)
    assert stats.timeline_cache_hits == 1
    assert stats.timeline_fetches == 0
    assert records[0].ready_at == meta["created_at"]

    moved = _meta(7, updated="2026-09-12T09:00:00Z")
    fetched: list[int] = []

    def fetch(_repo: str, number: int, _stats: object) -> list[object]:
        fetched.append(number)
        return []

    stats2 = pr_flow.RunStats()
    records2 = pr_flow.collect_records(
        [moved], days, _TZ, "r/x", cached, stats2, fetch_timeline_fn=fetch
    )
    assert fetched == [7]
    assert stats2.timeline_fetches == 1
    assert records2[0].updated_at == "2026-09-12T09:00:00Z"


def test_collect_records_falls_back_to_cache_then_partial_on_fetch_failure() -> None:
    days = [date(2026, 9, 12)]
    meta = _meta(8, updated="2026-09-12T09:00:00Z")
    cached = {
        "8": {
            "updated_at": "2026-09-12T03:00:00Z",
            "created_at": meta["created_at"],
            "merged_at": meta["merged_at"],
            "ready_at": meta["created_at"],
            "receipts": [],
            "head_deltas": 0,
            "partial": False,
        }
    }

    def boom(_repo, _number, _stats):
        raise pr_flow.PrFlowError("gh down")

    stats = pr_flow.RunStats()
    records = pr_flow.collect_records(
        [meta], days, _TZ, "r/x", cached, stats, fetch_timeline_fn=boom
    )
    assert stats.timeline_failures == 1
    assert records[0].ready_at == meta["created_at"]  # stale cached record kept
    assert records[0].partial is False

    records2 = pr_flow.collect_records([meta], days, _TZ, "r/x", {}, stats, fetch_timeline_fn=boom)
    assert records2[0].partial is True
    assert records2[0].ready_at == ""


# ── fetchers: pagination, stops, failure paths ──────────────────────────────


def test_fetch_closed_prs_stops_when_a_page_tail_predates_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    since = datetime(2026, 8, 15, tzinfo=UTC)  # time-bomb-ok: explicit fixture window input
    recent = [_meta(n, updated="2026-09-12T03:00:00Z") for n in range(100, 200)]
    tail_old = [_meta(n, updated="2026-09-12T03:00:00Z") for n in range(99)]
    tail_old.append(_meta(1, updated="2026-08-01T03:00:00Z"))
    pages = {1: recent, 2: tail_old, 3: "MUST NOT FETCH"}
    fetched: list[int] = []

    def run_gh(args: list[str], **_kwargs: object) -> str:
        page_no = int(args[1].rsplit("page=", 1)[1])
        fetched.append(page_no)
        return json.dumps(pages[page_no])

    monkeypatch.setattr(pr_flow, "_run_gh", run_gh)
    stats = pr_flow.RunStats()
    prs = pr_flow.fetch_closed_prs("r/x", since, stats)
    assert fetched == [1, 2]
    assert len(prs) == 200


def test_fetch_closed_prs_returns_on_a_short_page(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {1: [_meta(2, updated="2026-09-12T03:00:00Z")]}
    fetched: list[int] = []

    def run_gh(args: list[str], **_kwargs: object) -> str:
        page_no = int(args[1].rsplit("page=", 1)[1])
        fetched.append(page_no)
        return json.dumps(pages[page_no])

    monkeypatch.setattr(pr_flow, "_run_gh", run_gh)
    prs = pr_flow.fetch_closed_prs("r/x", datetime(2026, 8, 15, tzinfo=UTC), pr_flow.RunStats())
    assert fetched == [1]
    assert len(prs) == 1


def test_fetch_closed_prs_raises_when_the_walk_exceeds_its_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = [_meta(n, updated="2026-09-12T03:00:00Z") for n in range(100)]

    def run_gh(_args: list[str], **_kw: object) -> str:
        return json.dumps(page)

    monkeypatch.setattr(pr_flow, "_run_gh", run_gh)
    monkeypatch.setattr(pr_flow, "_MAX_LIST_PAGES", 2)
    with pytest.raises(pr_flow.PrFlowError, match="budget"):
        pr_flow.fetch_closed_prs("r/x", datetime(2026, 8, 15, tzinfo=UTC), pr_flow.RunStats())


def test_fetch_timeline_bounds_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    page = [{"event": "committed", "sha": "a" * 40, "commit_date": "2026-09-12T02:00:00Z"}] * 100

    def run_gh(_args: list[str], **_kw: object) -> str:
        return json.dumps(page)

    monkeypatch.setattr(pr_flow, "_run_gh", run_gh)
    stats = pr_flow.RunStats()
    events = pr_flow.fetch_timeline("r/x", 5, stats)
    assert len(events) == pr_flow._MAX_TIMELINE_PAGES * 100
    assert stats.gh_api_calls == pr_flow._MAX_TIMELINE_PAGES


def test_fetch_queue_depth_returns_none_on_trunk_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.SimpleNamespace(
        _trunk_post=lambda *_a, **_k: (None, "HTTP 503"),
        _trunk_target_payload=lambda _repo: {},
    )
    monkeypatch.setattr(pr_flow, "_trunk_client", lambda: contextlib.nullcontext(fake))
    stats = pr_flow.RunStats()
    assert pr_flow.fetch_queue_depth("r/x", "tok", stats) is None
    assert stats.trunk_error == "HTTP 503"


def test_fetch_quarantined_walks_next_page_token(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def trunk_post(
        endpoint: str, payload: dict[str, Any], token: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        calls.append(payload)
        if len(calls) == 1:
            return (
                {
                    "quarantined_tests": [
                        {"test_case_id": "a", "quarantined_at": "2026-09-12T01:00:00Z"}
                    ],
                    "page": {"next_page_token": "tok-2"},
                },
                None,
            )
        return ({"quarantined_tests": [{"test_case_id": "b"}], "page": {}}, None)

    fake = types.SimpleNamespace(
        _trunk_post=trunk_post,
        _trunk_target_payload=lambda _repo: {},
        _TRUNK_ORG_SLUG="org",
    )
    monkeypatch.setattr(pr_flow, "_trunk_client", lambda: contextlib.nullcontext(fake))
    stats = pr_flow.RunStats()
    result = pr_flow.fetch_quarantined("r/x", "tok", stats)
    assert result is not None and len(result) == 2
    assert calls[1]["page_query"] == {"page_size": 100, "page_token": "tok-2"}


def test_count_quarantined_by_day_converts_to_cluster_timezone() -> None:
    # 2026-09-09T18:00:00Z is 2026-09-10 02:00 in Asia/Shanghai.
    quarantined = [
        {"quarantined_at": "2026-09-09T18:00:00Z"},
        {"quarantined_at": "2026-09-10T01:00:00Z"},  # 09:00 CST, still 09-10
        {"quarantined_at": "bogus"},
        {"no_stamp": True},
    ]
    days = [date(2026, 9, 10)]
    counts = pr_flow.count_quarantined_by_day(quarantined, days, _TZ)
    assert counts == {date(2026, 9, 10): 2}


# ── snapshot persistence ────────────────────────────────────────────────────


def test_save_json_is_atomic_and_load_cache_heals_corruption(tmp_path: Path) -> None:
    target = tmp_path / "state" / "cache.json"
    pr_flow.save_json(target, {"version": 1, "prs": {"1": {"x": 1}}})
    assert pr_flow.load_cache(target)["1"]["x"] == 1

    target.write_text("{not json", encoding="utf-8")
    assert pr_flow.load_cache(target) == {}
    assert not list(target.parent.glob("*.tmp"))


def test_save_json_failed_replace_keeps_the_prior_file_and_no_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed rename must leave the previous snapshot intact and must not
    strand the temporary beside it -- the atomicity the write-and-rename
    actually buys (QA fold-in from the #2510 review)."""
    target = tmp_path / "state" / "cache.json"
    pr_flow.save_json(target, {"version": 1, "prs": {}})

    def fail_replace(_source: Path, _target: str | Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        pr_flow.save_json(target, {"version": 2, "prs": {"1": {"x": 1}}})

    assert json.loads(target.read_text(encoding="utf-8")) == {"version": 1, "prs": {}}
    assert list(target.parent.glob("*.tmp")) == []


def test_emit_snapshot_skips_the_pipeline_in_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted: list[object] = []
    monkeypatch.setattr(pr_flow, "_emit_events", emitted.append)

    snapshot = {"days": {}, "run": {}}
    pr_flow.emit_snapshot(snapshot, dry_run=True)
    assert emitted == []

    pr_flow.emit_snapshot(snapshot, dry_run=False)
    assert emitted == [snapshot]
