#!/usr/bin/env python3
"""Read SMS/iMessage 2FA codes and recent messages from macOS Messages.app.

Self-contained skill script — run from the repo source root with the venv Python:

    .venv/bin/python skills/sms/scripts/query.py --recent-codes
    .venv/bin/python skills/sms/scripts/query.py --recent-codes --phone-suffix 1118
    .venv/bin/python skills/sms/scripts/query.py --recent-messages --limit 10
    .venv/bin/python skills/sms/scripts/query.py --recent-codes --lookback-hours 24

macOS only. Reads ~/Library/Messages/chat.db (SQLite) directly, so the calling
process needs Full Disk Access. When TCC blocks that read the sqlite3
OperationalError propagates to the caller — there is no fallback (fail fast).
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

CHAT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"

# Apple epoch (2001-01-01 UTC) is 978307200 seconds after the Unix epoch.
APPLE_EPOCH_OFFSET = 978307200

DEFAULT_LOOKBACK_HOURS = 12

# Patterns for common 2FA verification codes.
# Each pattern captures one group: the code digits.
VERIFICATION_PATTERNS: list[re.Pattern[str]] = [
    # Chinese keywords
    re.compile(r"(?:验证码|校验码|动态码|短信验证码)\D{0,5}?(\d{4,8})"),
    # Korean keywords
    re.compile(r"(?:인증번호|인증\s*코드)\D{0,5}?(\d{4,8})"),
    # Japanese keywords
    re.compile(r"(?:認証コード|確認コード)\D{0,5}?(\d{4,8})"),
    # Google-style
    re.compile(r"G-(\d{4,8})\s+is\s+your", re.IGNORECASE),
    # English keywords
    re.compile(
        r"(?:verification|security|one.?time|OTP|auth(?:entication)?)\s*(?:code|pin|password)\D{0,5}?(\d{4,8})",
        re.IGNORECASE,
    ),
    # Generic "code: 123456"
    re.compile(r"(?:code|pin|密码)\D{0,5}?(\d{4,8})", re.IGNORECASE),
    # Standalone 6-digit sequences
    re.compile(r"(?:^|\s)(\d{6})(?:\s|$)", re.MULTILINE),
    # Square bracket codes
    re.compile(r"\[(\d{4,8})\]"),
    # "XXXXXX is your..." pattern
    re.compile(r"(\d{4,8})\s+is\s+your", re.IGNORECASE),
]


def _apple_epoch_to_datetime(apple_date: int) -> datetime:
    """Convert Apple's message date (ns or s since 2001-01-01) to a datetime."""
    if apple_date > 1_000_000_000_000_000:  # nanoseconds
        unix_ts = apple_date / 1_000_000_000 + APPLE_EPOCH_OFFSET
    else:  # seconds
        unix_ts = apple_date + APPLE_EPOCH_OFFSET
    return datetime.fromtimestamp(unix_ts, tz=UTC)


def _extract_codes(text: str) -> list[str]:
    """Extract candidate verification codes from message text, in first-seen order."""
    if not text:
        return []
    codes: list[str] = []
    seen: set[str] = set()
    for pattern in VERIFICATION_PATTERNS:
        for match in pattern.finditer(text):
            code = match.group(1)
            if code not in seen and len(code) >= 4:
                seen.add(code)
                codes.append(code)
    return codes


def _query_chat_db(
    phone_suffix: str | None,
    lookback_hours: float,
    limit: int,
    *,
    codes_only: bool,
) -> list[dict[str, Any]]:
    """Return recent SMS rows, newest first. When codes_only, keep only messages
    that carry a verification code — scanning a wider candidate window so the
    result can still reach `limit` code-bearing messages."""
    if not CHAT_DB_PATH.exists():
        raise FileNotFoundError(f"Messages database not found at {CHAT_DB_PATH}")

    cutoff_dt = datetime.now(tz=UTC) - timedelta(hours=lookback_hours)
    cutoff_ns = int((cutoff_dt.timestamp() - APPLE_EPOCH_OFFSET) * 1_000_000_000)
    scan_limit = max(limit * 5, 50) if codes_only else limit

    conn = sqlite3.connect(str(CHAT_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        query = """
            SELECT
                m.ROWID AS id,
                m.text,
                m.date,
                m.is_from_me,
                m.service,
                h.id AS phone
            FROM message m
            JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.service = 'SMS'
              AND m.date > ?
              AND m.text IS NOT NULL
              AND m.text != ''
        """
        params: list[Any] = [cutoff_ns]
        if phone_suffix:
            query += " AND h.id LIKE ?"
            params.append(f"%{phone_suffix}")
        query += " ORDER BY m.date DESC LIMIT ?"
        params.append(scan_limit)
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    results: list[dict[str, Any]] = []
    for row in rows:
        codes = _extract_codes(row["text"])
        if codes_only and not codes:
            continue
        results.append(
            {
                "id": row["id"],
                "text": row["text"],
                "phone": row["phone"],
                "date": _apple_epoch_to_datetime(row["date"]).isoformat(),
                "is_from_me": bool(row["is_from_me"]),
                "service": row["service"],
                "codes": codes,
            }
        )
        if len(results) >= limit:
            break
    return results


def query_codes(
    phone_suffix: str | None = None,
    lookback_hours: float = DEFAULT_LOOKBACK_HOURS,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return recent SMS messages that contain verification codes."""
    return _query_chat_db(phone_suffix, lookback_hours, limit, codes_only=True)


def query_messages(
    phone_suffix: str | None = None,
    lookback_hours: float = DEFAULT_LOOKBACK_HOURS,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return recent SMS messages."""
    return _query_chat_db(phone_suffix, lookback_hours, limit, codes_only=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Query SMS from Messages.app")
    parser.add_argument("--recent-codes", action="store_true", help="Get recent verification codes")
    parser.add_argument("--recent-messages", action="store_true", help="Get recent SMS messages")
    parser.add_argument("--phone-suffix", type=str, default=None, help="Filter by last N digits")
    parser.add_argument("--lookback-hours", type=float, default=DEFAULT_LOOKBACK_HOURS)
    parser.add_argument("--limit", type=int, default=20, help="Max messages")
    args = parser.parse_args()

    if args.recent_codes:
        results = query_codes(args.phone_suffix, args.lookback_hours, args.limit)
        for r in results:
            print(f"[{r['date']}] codes={' '.join(r['codes'])} | {r['phone']}: {r['text'][:100]}")
        if not results:
            print("No verification codes found.")
    elif args.recent_messages:
        results = query_messages(args.phone_suffix, args.lookback_hours, args.limit)
        for r in results:
            direction = "To" if r["is_from_me"] else "From"
            print(f"[{r['date']}] {direction} {r['phone']}: {r['text'][:200]}")
        if not results:
            print("No messages found.")
    else:
        parser.print_help(sys.stderr)


if __name__ == "__main__":
    main()
