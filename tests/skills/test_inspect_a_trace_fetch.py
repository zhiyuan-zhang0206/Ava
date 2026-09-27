"""Hermetic unit tests for the inspect-a-trace toolchain
(.agents/skills/inspect-a-trace/scripts/{fetch,read}_trace.py).

Locks the PR #637 follow-up nits: a malformed sibling span must not abort
the whole mirror scan, --trace-id validation must match its error text, and
unpadded base64 ids (legacy OTLP JSON) must decode. Pure logic only — no
network, no Tempo; the mirror walk uses tmp_path files.

Also locks the 2026-09-27 "unanchored checkout reaches production" fix:
`_mirror_dir` / `_cluster_secret` / `_gateway_get` resolve `$AVA_HOME` via
`shared.dotenv_boot.resolve_ava_home` instead of guessing `~/.ava` — an
unanchored checkout must never read a guessed home's `.env` or dial a
guessed gateway with its secret (see `tests/shared/test_unanchored_checkout.py`
for the same bug class against `shared.config` itself).

AVA_CLUSTER_SECRET is also a `shared.config.Settings` field alias, but
`read_trace.py` reads it straight from `os.environ` by design (the same
Settings-free stance as the rest of this skill script) — `monkeypatch.
setitem(os.environ, ...)` is used below instead of `monkeypatch.setenv` /
`delenv` so `lint_no_os_environ.py`'s real Settings-singleton-no-op check
stays meaningful for tests that DO exercise Settings.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_SCRIPTS_DIR = Path(__file__).parents[2] / ".agents" / "skills" / "inspect-a-trace" / "scripts"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ft = _load("fetch_trace_under_test", "fetch_trace.py")
rt = _load("read_trace_under_test", "read_trace.py")

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


# --- _mirror_dir: checkout-anchored home resolution, not a guessed ~/.ava ---


def test_mirror_dir_respects_explicit_override(tmp_path: Path) -> None:
    override = tmp_path / "custom-traces"
    args = SimpleNamespace(mirror_dir=str(override))

    assert ft._mirror_dir(args) == override


def test_mirror_dir_uses_resolve_ava_home_when_anchored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "dev-cluster-home"
    monkeypatch.setattr(ft, "resolve_ava_home", lambda: (home, True))

    assert ft._mirror_dir(SimpleNamespace(mirror_dir=None)) == home / "traces"


def test_mirror_dir_never_guesses_the_default_home_when_unanchored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-09-27 bug: no override, no anchored cluster -> the scratch
    home from `resolve_ava_home`, never a hardcoded `~/.ava`."""
    scratch = tmp_path / "ava-unanchored-deadbeefdeadbeef"
    monkeypatch.setattr(ft, "resolve_ava_home", lambda: (scratch, False))

    result = ft._mirror_dir(SimpleNamespace(mirror_dir=None))

    assert result == scratch / "traces"
    assert result != Path.home() / ".ava" / "traces"


# --- _cluster_secret: explicit env wins; an unanchored home is never read ---


def test_cluster_secret_prefers_explicit_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(os.environ, "AVA_CLUSTER_SECRET", "explicit-secret")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("AVA_CLUSTER_SECRET=file-secret\n")

    assert rt._cluster_secret(home, True) == "explicit-secret"
    assert rt._cluster_secret(home, False) == "explicit-secret"


def test_cluster_secret_reads_the_anchored_homes_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(os.environ, "AVA_CLUSTER_SECRET", raising=False)
    home = tmp_path / "dev-cluster-home"
    home.mkdir()
    (home / ".env").write_text("AVA_CLUSTER_SECRET=dev-cluster-secret\n")

    assert rt._cluster_secret(home, True) == "dev-cluster-secret"


def test_cluster_secret_never_reads_an_unanchored_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-09-27 bug: a guessed `~/.ava` (planted here as `home`) must
    never be read once the caller says this checkout does not own it."""
    monkeypatch.delitem(os.environ, "AVA_CLUSTER_SECRET", raising=False)
    home = tmp_path / "planted-prod-home"
    home.mkdir()
    (home / ".env").write_text("AVA_CLUSTER_SECRET=planted-prod-bearer\n")

    assert rt._cluster_secret(home, False) == ""


# --- _gateway_get: an unanchored checkout dials nothing without an explicit secret ---


class _NetworkDialedError(AssertionError):
    """Raised by the network guard below if `_gateway_get` reaches the network."""


def _forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _guard(*_args: object, **_kwargs: object) -> None:
        raise _NetworkDialedError("_gateway_get dialed the network")

    monkeypatch.setattr(urllib.request, "build_opener", _guard)


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _allow_network(monkeypatch: pytest.MonkeyPatch, seen_requests: list[Any]) -> None:
    class _FakeOpener:
        def open(self, req: Any, timeout: float | None = None) -> _FakeResponse:
            seen_requests.append(req)
            return _FakeResponse({"ok": True})

    def _build_opener(*_args: object, **_kwargs: object) -> _FakeOpener:
        return _FakeOpener()

    monkeypatch.setattr(urllib.request, "build_opener", _build_opener)


def test_gateway_get_refuses_when_unanchored_with_no_explicit_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(os.environ, "AVA_CLUSTER_SECRET", raising=False)
    scratch = tmp_path / "ava-unanchored-deadbeefdeadbeef"
    monkeypatch.setattr(rt, "resolve_ava_home", lambda: (scratch, False))
    _forbid_network(monkeypatch)

    with pytest.raises(SystemExit, match="unanchored"):
        rt._gateway_get("http://localhost:8000", "/api/events")


def test_gateway_get_proceeds_with_explicit_secret_even_when_unanchored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The explicit-env escape valve: a caller that knows what it is doing can
    still dial an unanchored checkout's gateway with its own secret."""
    monkeypatch.setitem(os.environ, "AVA_CLUSTER_SECRET", "explicit-secret")
    scratch = tmp_path / "ava-unanchored-deadbeefdeadbeef"
    monkeypatch.setattr(rt, "resolve_ava_home", lambda: (scratch, False))
    seen: list[Any] = []
    _allow_network(monkeypatch, seen)

    result = rt._gateway_get("http://localhost:8000", "/api/events")

    assert result == {"ok": True}
    assert len(seen) == 1
    assert seen[0].get_header("Authorization") == "Bearer explicit-secret"


def test_gateway_get_proceeds_when_anchored_using_the_homes_env_file_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(os.environ, "AVA_CLUSTER_SECRET", raising=False)
    home = tmp_path / "dev-cluster-home"
    home.mkdir()
    (home / ".env").write_text("AVA_CLUSTER_SECRET=dev-cluster-secret\n")
    monkeypatch.setattr(rt, "resolve_ava_home", lambda: (home, True))
    seen: list[Any] = []
    _allow_network(monkeypatch, seen)

    result = rt._gateway_get("http://localhost:8000", "/api/events")

    assert result == {"ok": True}
    assert seen[0].get_header("Authorization") == "Bearer dev-cluster-secret"
