"""Hermetic unit tests for the sms skill (ava_builtins/skills/sms/scripts/query.py).

The skill's live behavior (reading the real ~/Library/Messages/chat.db under
macOS Full Disk Access) can't run in CI — that path is verified by hand. These
lock the *pure* logic that would regress silently: the code-extraction patterns,
the codes-only filtering, the lookback cutoff, the phone-suffix filter, and the
returned row shape — by pointing the module at a synthetic SQLite database with
the same message/handle schema shape, so nothing touches the user's real data.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_PATH = Path(__file__).parents[2] / "ava_builtins" / "skills" / "sms" / "scripts" / "query.py"
_spec = importlib.util.spec_from_file_location("sms_query_under_test", _PATH)
assert _spec and _spec.loader
sms = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sms
_spec.loader.exec_module(sms)


def _apple_ns(dt: datetime) -> int:
    return int((dt.timestamp() - sms.APPLE_EPOCH_OFFSET) * 1_000_000_000)


@pytest.fixture
def chat_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthetic chat.db with the columns query.py reads, pointed at by CHAT_DB_PATH."""
    db = tmp_path / "chat.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, date INTEGER,
          is_from_me INTEGER, service TEXT, handle_id INTEGER);
        """
    )
    conn.execute("INSERT INTO handle (ROWID, id) VALUES (1, '+15551231118')")
    now = datetime.now(tz=UTC)
    rows = [
        ("【微信】验证码 654321，请勿泄露", now - timedelta(minutes=5), 0, "SMS", 1),
        ("G-778899 is your Google verification code", now - timedelta(minutes=10), 0, "SMS", 1),
        ("hey are we still on for lunch?", now - timedelta(minutes=15), 0, "SMS", 1),
        ("old code 111111", now - timedelta(hours=48), 0, "SMS", 1),  # outside default lookback
    ]
    for text, dt, is_from_me, service, handle_id in rows:
        conn.execute(
            "INSERT INTO message (text, date, is_from_me, service, handle_id) VALUES (?, ?, ?, ?, ?)",
            (text, _apple_ns(dt), is_from_me, service, handle_id),
        )
    conn.commit()
    conn.close()
    monkeypatch.setattr(sms, "CHAT_DB_PATH", db)
    return db


def test_extract_codes_multilingual() -> None:
    assert sms._extract_codes("【微信】验证码 654321，请勿泄露") == ["654321"]
    assert sms._extract_codes("G-778899 is your Google verification code") == ["778899"]
    assert sms._extract_codes("hey are we still on for lunch?") == []


def test_query_codes_returns_only_code_bearing_rows(chat_db: Path) -> None:
    codes = sms.query_codes()
    assert [c["codes"] for c in codes] == [
        ["654321"],
        ["778899"],
    ]  # newest first, plain msg dropped
    assert all(
        set(c) >= {"id", "text", "phone", "date", "is_from_me", "service", "codes"} for c in codes
    )


def test_query_messages_respects_lookback(chat_db: Path) -> None:
    # 3 within the 12h default; the 48h-old row is excluded.
    assert len(sms.query_messages()) == 3
    # Widen the window and the old row appears.
    assert len(sms.query_messages(lookback_hours=72)) == 4


def test_phone_suffix_filter(chat_db: Path) -> None:
    assert len(sms.query_messages(phone_suffix="1118")) == 3
    assert sms.query_messages(phone_suffix="9999") == []


def test_missing_database_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sms, "CHAT_DB_PATH", tmp_path / "nope.db")
    with pytest.raises(FileNotFoundError):
        sms.query_codes()
