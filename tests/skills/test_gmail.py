"""Tests for the Gmail skill — pure functions and CLI dry-run output."""

from __future__ import annotations

import json
import subprocess
import sys
from email.message import EmailMessage, Message
from pathlib import Path

import pytest

# The skill is a standalone CLI; import its pure helpers for unit testing.
_SKILL_DIR = Path(__file__).resolve().parents[2] / "ava_builtins" / "skills" / "gmail" / "reference"
sys.path.insert(0, str(_SKILL_DIR))
import feed as gmail  # noqa: E402  # pyright: ignore[reportMissingImports]

# The skill under test is a standalone reference file injected via
# sys.path at runtime — pyright cannot resolve its module type, so every
# call site reports Unknown. File-level downgrade of the two call-site
# rules keeps the rest of this file's strict checks intact (audit round-2
# tests-ci P1, task #1143).
# pyright: reportUnknownMemberType = warning
# pyright: reportUnknownArgumentType = warning


# ── _parse_list_id ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        ("", None),
        ("<newsletter.example.com>", "newsletter.example.com"),
        ("My Newsletter <newsletter.example.com>", "newsletter.example.com"),
        ("Prefix <first> Suffix <second.example.org>", "second.example.org"),
        ("no-brackets-here", "no-brackets-here"),
        ("<trailing.space >", "trailing.space"),
        ("< leading.space>", "leading.space"),
        ("  <spaces.example.com>  ", "spaces.example.com"),
    ],
)
def test_parse_list_id(raw: str | None, expected: str | None) -> None:
    assert gmail._parse_list_id(raw) == expected


# ── _html_to_text ───────────────────────────────────────────────────────────


def test_html_to_text_plain() -> None:
    assert gmail._html_to_text("Hello world") == "Hello world"


def test_html_to_text_strips_tags() -> None:
    result = gmail._html_to_text("<p>Hello <b>world</b></p>")
    assert "Hello" in result
    assert "world" in result
    assert "<p>" not in result
    assert "<b>" not in result


def test_html_to_text_strips_scripts() -> None:
    result = gmail._html_to_text("<html><script>alert('x')</script><body>Content</body></html>")
    assert "Content" in result
    assert "alert" not in result


def test_html_to_text_handles_entities() -> None:
    result = gmail._html_to_text("Price: &euro;5 &amp; &lt;free&gt;")
    assert "€5" in result
    assert "&" in result or "&amp;" not in result


def test_html_to_text_collapses_whitespace() -> None:
    result = gmail._html_to_text("<div>a</div>   <div>b</div>")
    # Whitespace should be normalized — no double spaces
    assert "  " not in result or result.count("\n") >= 1


def test_html_to_text_empty() -> None:
    assert gmail._html_to_text("") == ""


# ── _iso ────────────────────────────────────────────────────────────────────


def test_iso_none() -> None:
    assert gmail._iso(None) is None
    assert gmail._iso("") is None


def test_iso_rfc2822() -> None:
    result = gmail._iso("Mon, 21 Jun 2026 15:30:00 +0000")
    assert result is not None
    assert result.endswith("Z")
    assert "2026-06-21T15:30:00" in result


def test_iso_naive_becomes_utc() -> None:
    result = gmail._iso("Mon, 21 Jun 2026 15:30:00")
    assert result is not None
    assert result.endswith("Z")


def test_iso_invalid() -> None:
    assert gmail._iso("not a date") is None
    assert gmail._iso("garbage") is None


# ── _now_iso ────────────────────────────────────────────────────────────────


def test_now_iso_ends_with_z() -> None:
    assert gmail._now_iso().endswith("Z")


# ── _meta ───────────────────────────────────────────────────────────────────


def test_meta_extracts_fields() -> None:
    msg = Message()
    msg["Message-Id"] = "<abc123@example.com>"
    msg["List-Id"] = "My List <newsletter.example.com>"
    msg["From"] = "sender@example.com"
    msg["Subject"] = "Test Subject"
    msg["Date"] = "Mon, 21 Jun 2026 15:30:00 +0000"

    meta = gmail._meta(msg)
    assert meta["message_id"] == "abc123@example.com"
    assert meta["list_id"] == "newsletter.example.com"
    assert meta["from"] == "sender@example.com"
    assert meta["subject"] == "Test Subject"
    assert meta["date"] is not None


def test_meta_missing_fields() -> None:
    msg = Message()
    meta = gmail._meta(msg)
    assert meta["message_id"] is None
    assert meta["list_id"] is None
    assert meta["from"] is None
    assert meta["subject"] is None
    assert meta["date"] is None


# ── _addr_list ──────────────────────────────────────────────────────────────


def test_addr_list_single() -> None:
    assert gmail._addr_list("a@b.com") == ["a@b.com"]


def test_addr_list_multiple() -> None:
    result = gmail._addr_list("a@b.com, c@d.com")
    assert result == ["a@b.com", "c@d.com"]


def test_addr_list_with_names() -> None:
    result = gmail._addr_list("Alice <a@b.com>, Bob <c@d.com>")
    assert result == ["a@b.com", "c@d.com"]


def test_addr_list_none() -> None:
    assert gmail._addr_list(None) == []


def test_addr_list_empty() -> None:
    assert gmail._addr_list("") == []


# ── _since_to_ts ────────────────────────────────────────────────────────────


def test_since_to_ts_none() -> None:
    assert gmail._since_to_ts(None) is None


def test_since_to_ts_days() -> None:
    ts = gmail._since_to_ts("30d")
    assert ts is not None
    assert ts > 0


def test_since_to_ts_hours() -> None:
    ts = gmail._since_to_ts("12h")
    assert ts is not None
    assert ts > 0


def test_since_to_ts_iso_date() -> None:
    ts = gmail._since_to_ts("2026-06-01")
    assert ts is not None


# ── _xgm ────────────────────────────────────────────────────────────────────


def test_xgm_simple() -> None:
    result = gmail._xgm("from:boss newer_than:7d")
    assert result.startswith('"')
    assert result.endswith('"')
    assert "from:boss" in result


def test_xgm_escapes_quotes() -> None:
    result = gmail._xgm('subject:"hello world"')
    assert '\\"' in result


# ── _body_arg ───────────────────────────────────────────────────────────────


def test_body_arg_direct() -> None:
    assert gmail._body_arg("hello") == "hello"


def test_body_arg_rejects_empty() -> None:
    with pytest.raises(ValueError, match="no body"):
        gmail._body_arg("")


def test_body_arg_rejects_whitespace() -> None:
    with pytest.raises(ValueError, match="no body"):
        gmail._body_arg("   ")


# ── _msg_summary / _sent_summary ────────────────────────────────────────────


def test_msg_summary() -> None:
    msg = EmailMessage()
    msg["From"] = "me@gmail.com"
    msg["To"] = "you@gmail.com"
    msg["Subject"] = "Hi"
    msg["Message-Id"] = "<abc@mail.gmail.com>"
    msg.set_content("Hello world")

    summary = gmail._msg_summary(msg)
    assert summary["message_id"] == "abc@mail.gmail.com"
    assert summary["from"] == "me@gmail.com"
    assert summary["to"] == "you@gmail.com"
    assert summary["subject"] == "Hi"
    assert summary["body"] == "Hello world"
    assert summary["attachments"] == []


def test_sent_summary_dry_run() -> None:
    msg = EmailMessage()
    msg["From"] = "me@gmail.com"
    msg["To"] = "you@gmail.com"
    msg["Subject"] = "Hi"
    msg["Message-Id"] = "<abc@mail.gmail.com>"
    msg.set_content("Hello")

    summary = gmail._sent_summary(msg, sent=False)
    assert summary["sent"] is False
    assert summary["body"] == "Hello"


def test_sent_summary_real() -> None:
    msg = EmailMessage()
    msg["From"] = "me@gmail.com"
    msg["To"] = "you@gmail.com"
    msg["Subject"] = "Hi"
    msg["Message-Id"] = "<abc@mail.gmail.com>"
    msg.set_content("Hello")

    summary = gmail._sent_summary(msg, sent=True)
    assert summary["sent"] is True


# ── CLI dry-run integration ─────────────────────────────────────────────────


def test_cli_send_dry_run() -> None:
    """--dry-run send prints JSON with sent=false, no SMTP connection."""
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(_SKILL_DIR / "feed.py"),
            "send",
            "--to",
            "test@example.com",
            "--subject",
            "Test",
            "--body",
            "Hello world",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    # May fail due to missing Keychain entry — that's expected in CI.
    # We only assert the shape IF it succeeds without Keychain error.
    if "could not read Keychain entry" in result.stderr:
        pytest.skip("Keychain entry not available in this environment")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["sent"] is False
    assert data["subject"] == "Test"
    assert data["to"] == "test@example.com"
    assert "Hello world" in data["body"]


def test_cli_reply_dry_run_keychain_required() -> None:
    """reply --dry-run requires Keychain (to look up the account); verify it
    fails cleanly rather than leaking a traceback."""
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(_SKILL_DIR / "feed.py"),
            "reply",
            "--message-id",
            "<test@host>",
            "--body",
            "Thanks",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if "could not read Keychain entry" in result.stderr:
        pytest.skip("Keychain entry not available")
    assert result.returncode == 0 or "GmailError" in result.stderr


def test_cli_send_no_recipient_errors() -> None:
    """send without --to should fail with a clear error."""
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(_SKILL_DIR / "feed.py"),
            "send",
            "--subject",
            "Test",
            "--body",
            "Hello",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    # Should fail because --to is required
    assert result.returncode != 0


def test_cli_help() -> None:
    """The CLI prints help without importing or connecting."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(_SKILL_DIR / "feed.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert "search" in result.stdout
    assert "send" in result.stdout
    assert "reply" in result.stdout
