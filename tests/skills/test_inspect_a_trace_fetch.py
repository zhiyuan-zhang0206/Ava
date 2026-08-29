"""Hermetic unit tests for the inspect-a-trace fetch toolchain
(.agents/skills/inspect-a-trace/scripts/fetch_trace.py).

Locks the PR #637 follow-up nits: a malformed sibling span must not abort
the whole mirror scan, --trace-id validation must match its error text, and
unpadded base64 ids (legacy OTLP JSON) must decode. Pure logic only — no
network, no Tempo; the mirror walk uses tmp_path files.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_PATH = (
    Path(__file__).parents[2]
    / ".agents"
    / "skills"
    / "inspect-a-trace"
    / "scripts"
    / "fetch_trace.py"
)
_spec = importlib.util.spec_from_file_location("fetch_trace_under_test", _PATH)
assert _spec and _spec.loader
ft = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ft
_spec.loader.exec_module(ft)

_TRACE = "00112233445566778899aabbccddeeff"
_OTHER = "ffeeddccbbaa99887766554433221100"
_SPAN_A = "0102030405060708"
_SPAN_B = "1112131415161718"
_SPAN_C = "2122232425262728"
_SPAN_D = "3132333435363738"


def _span(
    span_id: str,
    name: str = "op",
    trace_id: str | None = _TRACE,
    parent: str | None = None,
) -> dict[str, Any]:
    sp: dict[str, Any] = {
        "spanId": span_id,
        "name": name,
        "kind": "SPAN_KIND_INTERNAL",
        "startTimeUnixNano": "1000",
        "endTimeUnixNano": "2000",
        "status": {"code": "STATUS_CODE_OK"},
        "attributes": [],
    }
    if trace_id is not None:
        sp["traceId"] = trace_id
    if parent is not None:
        sp["parentSpanId"] = parent
    return sp


def _envelope(*spans: dict[str, Any]) -> dict[str, Any]:
    return {"resourceSpans": [{"scopeSpans": [{"spans": list(spans)}]}]}


# --- _id_to_hex ---


def test_id_to_hex_normalizes_hex_and_lowercases() -> None:
    assert ft._id_to_hex(_TRACE) == _TRACE
    assert ft._id_to_hex(_TRACE.upper()) == _TRACE
    assert ft._id_to_hex(_SPAN_B.upper()) == _SPAN_B


def test_id_to_hex_decodes_padded_base64() -> None:
    assert ft._id_to_hex("ABEiM0RVZneImaq7zN3u/w==") == _TRACE
    assert ft._id_to_hex("qrvM3e7/ABE=") == "aabbccddeeff0011"


def test_id_to_hex_decodes_unpadded_base64() -> None:
    """Legacy protojson without padding: 22-char trace id, 11-char span id."""
    assert ft._id_to_hex("ABEiM0RVZneImaq7zN3u/w") == _TRACE
    assert ft._id_to_hex("qrvM3e7/ABE") == "aabbccddeeff0011"


@pytest.mark.parametrize(
    "bad",
    ["", "0" * 31, "0" * 33, "g" * 32, "!" * 24, "zzzzzzzzzzzzzzzzzzzzzzzz"],
)
def test_id_to_hex_refuses_malformed_values(bad: str) -> None:
    with pytest.raises(ValueError, match="malformed span id"):
        ft._id_to_hex(bad)


# --- _mirror_day ---


def test_mirror_day_parses_rotated_and_legacy_names() -> None:
    assert ft._mirror_day(Path("spans-2026-08-25T16-14-01.621-size.jsonl")) == date(2026, 8, 25)
    assert ft._mirror_day(Path("spans-2026-08-25T16-14-01.621-time.jsonl")) == date(2026, 8, 25)
    assert ft._mirror_day(Path("spans-20250825-12345.jsonl")) == date(2025, 8, 25)
    assert ft._mirror_day(Path("spans.jsonl")) is None


def test_mirror_day_returns_none_for_unparsable_dates() -> None:
    assert ft._mirror_day(Path("spans-2026-13-99T00-00-00.000-size.jsonl")) is None
    assert ft._mirror_day(Path("spans-hello.jsonl")) is None


# --- _spans_from_envelope: malformed siblings must not abort the scan ---


def test_malformed_sibling_with_empty_trace_id_is_skipped() -> None:
    data = _envelope(
        _span(_SPAN_A),
        _span("", trace_id=""),  # empty traceId — old code crashed here
        _span(_SPAN_B),
    )

    spans = ft._spans_from_envelope(data, _TRACE)

    assert [s["span_id"] for s in spans] == [_SPAN_A, _SPAN_B]


def test_malformed_sibling_missing_trace_id_is_skipped() -> None:
    bad = _span(_SPAN_A)
    del bad["traceId"]
    data = _envelope(_span(_SPAN_B), bad, _span(_SPAN_C))

    spans = ft._spans_from_envelope(data, _TRACE)

    assert [s["span_id"] for s in spans] == [_SPAN_B, _SPAN_C]


def test_malformed_target_span_is_skipped_but_siblings_kept() -> None:
    """A span with the right traceId but a broken spanId is dropped, not fatal."""
    data = _envelope(_span(_SPAN_A), _span("bad", trace_id=_TRACE), _span(_SPAN_B))

    spans = ft._spans_from_envelope(data, _TRACE)

    assert [s["span_id"] for s in spans] == [_SPAN_A, _SPAN_B]


def test_other_trace_spans_are_filtered_out() -> None:
    data = _envelope(_span(_SPAN_A), _span(_SPAN_B, trace_id=_OTHER))

    spans = ft._spans_from_envelope(data, _TRACE)

    assert [s["span_id"] for s in spans] == [_SPAN_A]


# --- cmd_fetch --trace-id validation ---


def _fetch_args(trace_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        trace_id=trace_id,
        source="mirror",
        out="trace_raw.json",
        days=2,
        mirror_dir=None,
        tempo_url="http://localhost:3200",
    )


def test_cmd_fetch_rejects_wrong_length_ids(capsys: pytest.CaptureFixture[str]) -> None:
    for bad in ("a" * 30, "z" * 32):
        assert ft.cmd_fetch(_fetch_args(bad)) == 2
        assert "--trace-id must be a 31- or 32-char hex id" in capsys.readouterr().out


def test_cmd_fetch_accepts_31_and_32_char_ids_and_zfills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def fake_fetch(_args: Any, trace_id: str) -> list[dict[str, Any]]:
        seen.append(trace_id)
        return []

    monkeypatch.setattr(ft, "fetch_from_mirror", fake_fetch)
    monkeypatch.setattr(ft, "fetch_from_tempo", fake_fetch)

    assert ft.cmd_fetch(_fetch_args("a" * 31)) == 1  # no spans -> not found
    assert ft.cmd_fetch(_fetch_args("b" * 32)) == 1
    assert seen == ["0" + "a" * 31, "b" * 32]


# --- fetch_from_mirror: cross-rotation merge + malformed lines ---


def test_fetch_from_mirror_merges_across_rotation_and_skips_bad_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    active = tmp_path / "spans.jsonl"
    # Relative date: the retention window slides with the wall clock — a hard
    # coded date becomes a CI time bomb (2026-09-24 for 2026-08-25 + days=30).
    rotated_day = datetime.now(UTC).date() - timedelta(days=1)
    rotated = tmp_path / f"spans-{rotated_day}T16-14-01.621-size.jsonl"
    legacy_unpadded = ft._trace_id_b64(_TRACE).rstrip("=")
    active.write_text(
        chr(10).join(
            [
                json.dumps(_envelope(_span(_SPAN_A), _span(_SPAN_B, trace_id=_OTHER))),
                json.dumps(_envelope(_span(_SPAN_D, trace_id=legacy_unpadded))),
                "not-json-at-all",
            ]
        )
        + chr(10),
        encoding="utf-8",
    )
    rotated.write_text(
        json.dumps(
            _envelope(
                _span(_SPAN_A),  # duplicate of A: same span_id, deduped
                _span(_SPAN_C),
                _span("", trace_id=""),  # malformed sibling — must not abort
            )
        )
        + chr(10),
        encoding="utf-8",
    )
    args = SimpleNamespace(mirror_dir=str(tmp_path), days=30)

    spans = ft.fetch_from_mirror(args, _TRACE)
    capsys.readouterr()

    assert sorted(s["span_id"] for s in spans) == sorted([_SPAN_A, _SPAN_C, _SPAN_D])
