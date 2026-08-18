"""The persistent heal record — shared machinery, previously untested.

`ops/controllers/_heal_record.py` is the backoff three acting controllers keep across
the restart their own heal causes. It had no tests of its own, which mattered here
because #1074 adds a fourth writer to it (`check_pin_drift`'s bare-`Exception` branch,
which recorded nothing and so armed no backoff at all).

Two properties carry the weight and neither is obvious from the call sites:

- **`consecutive_failures` counts rounds that could not heal**, which is the number an
  operator reads as "how long has this host been stuck". It carries over only across
  failures toward the SAME target; a success or a new target restarts it. Getting that
  wrong in either direction is the bug PR #879 fixed once already — a heal that only
  recorded successes never armed its own backoff and retried forever.
- **The reader normalizes two legacy shapes** onto today's `target` key. A record
  written by an older process must not read as "no record", because "no record" means
  "not in backoff" — i.e. an upgrade would silently un-arm every controller's backoff
  exactly once, at the moment a restart loop is most likely.

Everything is a real file under `tmp_path`; the module is pure filesystem + clock.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ops.controllers import _heal_record

_TARGET = "abc1234abc1234"
_OTHER = "def5678def5678"


def _rec(path: Path) -> dict[str, object]:
    parsed = _heal_record.read_record(path)
    assert parsed is not None
    return parsed


# ── the backoff window ──────────────────────────────────────────────────────


def test_no_record_is_not_in_backoff(tmp_path: Path) -> None:
    """The absent case has to be permissive: a host that has never attempted a heal
    must be free to attempt one."""
    assert _heal_record.in_backoff(tmp_path / "missing", _TARGET, 1800.0) is False


def test_a_recent_attempt_toward_this_target_is_in_backoff(tmp_path: Path) -> None:
    path = tmp_path / "heal"
    _heal_record.record_attempt(path, _TARGET, ok=True)
    assert _heal_record.in_backoff(path, _TARGET, 1800.0) is True


def test_a_different_target_is_not_in_backoff(tmp_path: Path) -> None:
    """The record is keyed on where the heal was going. A pin that has since moved is
    a different heal, and holding it off on the old one's record would strand the host
    on a target nothing is trying to reach."""
    path = tmp_path / "heal"
    _heal_record.record_attempt(path, _OTHER, ok=False)
    assert _heal_record.in_backoff(path, _TARGET, 1800.0) is False


def test_an_expired_record_is_not_in_backoff(tmp_path: Path) -> None:
    path = tmp_path / "heal"
    path.write_text(json.dumps({"target": _TARGET, "ts": time.time() - 3600.0, "ok": False}))
    assert _heal_record.in_backoff(path, _TARGET, 1800.0) is False


def test_an_unreadable_record_is_not_in_backoff(tmp_path: Path) -> None:
    """Corrupt or truncated content must not wedge a controller into permanent
    backoff — the failure mode would be a host that never heals again and says
    nothing about why."""
    path = tmp_path / "heal"
    path.write_text("{not json")
    assert _heal_record.read_record(path) is None
    assert _heal_record.in_backoff(path, _TARGET, 1800.0) is False

    path.write_text(json.dumps({"target": _TARGET, "ts": "not-a-number"}))
    assert _heal_record.in_backoff(path, _TARGET, 1800.0) is False


# ── consecutive_failures ────────────────────────────────────────────────────


def test_consecutive_failures_accumulate_toward_one_target(tmp_path: Path) -> None:
    """The count an operator reads as "how long has this host been stuck"."""
    path = tmp_path / "heal"
    for expected in (1, 2, 3):
        _heal_record.record_attempt(path, _TARGET, ok=False, error=f"attempt {expected}")
        assert _rec(path)["consecutive_failures"] == expected
    assert _rec(path)["last_error"] == "attempt 3"
    assert _rec(path)["ok"] is False


def test_a_success_restarts_the_count(tmp_path: Path) -> None:
    path = tmp_path / "heal"
    _heal_record.record_attempt(path, _TARGET, ok=False, error="boom")
    _heal_record.record_attempt(path, _TARGET, ok=True)
    assert _rec(path)["consecutive_failures"] == 0
    assert _rec(path)["ok"] is True


def test_a_new_target_restarts_the_count(tmp_path: Path) -> None:
    """Failures toward a pin that has since moved say nothing about the new one."""
    path = tmp_path / "heal"
    _heal_record.record_attempt(path, _TARGET, ok=False)
    _heal_record.record_attempt(path, _TARGET, ok=False)
    _heal_record.record_attempt(path, _OTHER, ok=False)
    assert _rec(path)["consecutive_failures"] == 1
    assert _rec(path)["target"] == _OTHER


def test_a_failure_is_recorded_at_all(tmp_path: Path) -> None:
    """The property PR #879 added and #1074 found missing from one more branch: a heal
    that never succeeds must still leave a record, because the record IS the backoff.
    Without it the controller retries at its cooldown cadence forever."""
    path = tmp_path / "heal"
    _heal_record.record_attempt(path, _TARGET, ok=False, error="spawn failed")
    assert _heal_record.in_backoff(path, _TARGET, 1800.0) is True


def test_record_creates_its_parent_directory(tmp_path: Path) -> None:
    """`$AVA_HOME` exists on a live host, but a controller must not crash its round on
    a layout that has not been converged yet."""
    path = tmp_path / "nested" / "deeper" / "heal"
    _heal_record.record_attempt(path, _TARGET, ok=True)
    assert path.exists()


# ── legacy shapes, and clearing ─────────────────────────────────────────────


def test_the_legacy_pin_key_reads_as_target(tmp_path: Path) -> None:
    """Records written before this module was extracted key the commit as `pin`. They
    must not read as "no record", which means "not in backoff" — an upgrade would
    otherwise un-arm every controller's backoff exactly once."""
    path = tmp_path / "heal"
    path.write_text(json.dumps({"pin": _TARGET, "ts": time.time(), "ok": False}))
    assert _rec(path)["target"] == _TARGET
    assert _heal_record.in_backoff(path, _TARGET, 1800.0) is True


def test_the_legacy_plaintext_form_reads_as_a_successful_attempt(tmp_path: Path) -> None:
    """The oldest form is `<sha>\\n<ts>`, which carries no outcome — read as a success
    with no failure history, which is the conservative reading (it arms the backoff
    without inventing a failure count)."""
    path = tmp_path / "heal"
    path.write_text(f"{_TARGET}\n{time.time()}\n")
    rec = _rec(path)
    assert rec["target"] == _TARGET
    assert rec["ok"] is True
    assert rec["consecutive_failures"] == 0
    assert _heal_record.in_backoff(path, _TARGET, 1800.0) is True


def test_a_malformed_plaintext_record_reads_as_absent(tmp_path: Path) -> None:
    path = tmp_path / "heal"
    path.write_text(f"{_TARGET}\nnot-a-timestamp\n")
    assert _heal_record.read_record(path) is None
    path.write_text(_TARGET)  # no timestamp line at all
    assert _heal_record.read_record(path) is None


def test_clear_drops_the_record_and_is_idempotent(tmp_path: Path) -> None:
    """Called on the one reading that proves a heal is no longer needed — the
    dimension converged — so the backoff must not outlive it."""
    path = tmp_path / "heal"
    _heal_record.record_attempt(path, _TARGET, ok=False)
    _heal_record.clear(path)
    assert not path.exists()
    assert _heal_record.in_backoff(path, _TARGET, 1800.0) is False
    _heal_record.clear(path)  # absent is not an error
