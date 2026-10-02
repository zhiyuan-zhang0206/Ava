"""Gmail skill -- one self-contained CLI: read over IMAP, send over SMTP.

The agent runs this as a subprocess (no SDK import, no browser), e.g.

    .venv/bin/python skills/gmail/scripts/feed.py search   --query "from:foo newer_than:7d"
    .venv/bin/python skills/gmail/scripts/feed.py read     --message-id "<abc@host>"
    .venv/bin/python skills/gmail/scripts/feed.py discover  --since 60d
    .venv/bin/python skills/gmail/scripts/feed.py enum      --list-id newsletter.example.com --since 30d
    .venv/bin/python skills/gmail/scripts/feed.py sync      --list-id newsletter.example.com --since 30d
    .venv/bin/python skills/gmail/scripts/feed.py send      --to a@b.com --subject Hi --body "..." --attach /p/f.pdf --dry-run
    .venv/bin/python skills/gmail/scripts/feed.py reply     --message-id "<abc@host>" --body "..." --dry-run
    .venv/bin/python skills/gmail/scripts/feed.py forward   --message-id "<abc@host>" --to a@b.com --dry-run
    .venv/bin/python skills/gmail/scripts/feed.py draft     --to a@b.com --subject Hi --body "..."
    .venv/bin/python skills/gmail/scripts/feed.py draft-delete --message-id "<abc@host>"

and reads the JSON printed to stdout. Eleven lenses (read over IMAP, write over
SMTP, drafts over IMAP APPEND):

  search  -- run a Gmail search, return matching messages' metadata.
  read    -- return one/few matching messages' extracted text (headers + body).
  discover-- list the distinct newsletter List-Ids seen in a time window (seed S2).
  enum    -- list one newsletter's (one List-Id's) new issues, newest-first.
  fetch   -- pull one message's .eml + attachments into the raw mirror, projected to S1.
  sync    -- enum a List-Id's new issues, fetch each, print the S1 items.
  send    -- compose and send a new email, optional file attachments (SIDE-EFFECTFUL; --dry-run to preview).
  reply   -- reply to a message by Message-Id, threaded (SIDE-EFFECTFUL; --dry-run).
  forward -- forward a message by Message-Id, original attachments carried along (SIDE-EFFECTFUL; --dry-run).
  draft   -- compose into the Drafts folder, nothing sent (editable/sendable from any Gmail client).
  draft-delete -- remove a draft from the Drafts folder by its Message-Id.

The mailbox + secret come from the macOS Keychain (one generic-password entry,
service `ava-gmail-imap`): the account label is the login username, the secret is
a Gmail App Password (2FA-gated; the same password works for both IMAP and SMTP;
IMAP must be enabled in Gmail settings).

Detection: a message is a newsletter iff it carries a `List-Id` header -- the
durable per-newsletter identity (the From address rotates, the List-Id does not).
`category:` / `list:` / `after:` ride Gmail's `X-GM-RAW` IMAP extension, so the
full Gmail search syntax works over IMAP. Pure stdlib (`imaplib` + `smtplib` +
`email`).

`send` / `reply` / `forward` put real mail on the wire -- use `--dry-run` to
compose and inspect the message (headers + body) without sending; the caller
confirms before a real send. `draft` never sends: it APPENDs the composed
message into the mailbox's Drafts folder (found via its RFC 6154 `\\Drafts`
special-use attribute), where any Gmail client can edit and send it;
`draft-delete` removes it again by Message-Id.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

# `imap`/`smtp` are sibling files in this same scripts/ dir, not an importable
# package (PYTHONSAFEPATH=1 keeps a script's own directory off sys.path) —
# restore it for the sibling import. Structure Rule 6 recognizes this exact
# __file__-derived, within-skill shape as the endorsed guard.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# re-exported below (SKILL.md invokes the CLI; tests import feed.*): named
# explicitly with `as` self-aliases so pyright recognizes them as intentional,
# not unused, imports (PEP 484 convention).
from imap import ALL_MAIL as ALL_MAIL
from imap import DISCOVER_NET as DISCOVER_NET
from imap import IMAP_HOST as IMAP_HOST
from imap import IMAP_PORT as IMAP_PORT
from imap import KEYCHAIN_SERVICE as KEYCHAIN_SERVICE
from imap import MIRROR_ROOT as MIRROR_ROOT
from imap import GmailError as GmailError

# used directly below in the argparse dispatch.
from imap import discover, enum, fetch, read, search, sync, to_s1
from imap import html_to_text as html_to_text
from imap import iso as iso
from imap import meta as meta
from imap import now_iso as now_iso
from imap import parse_list_id as parse_list_id
from imap import save as save
from imap import xgm as xgm
from smtp import SMTP_HOST as SMTP_HOST
from smtp import SMTP_PORT as SMTP_PORT
from smtp import addr_list as addr_list
from smtp import draft, draft_delete, forward, reply, send
from smtp import msg_summary as msg_summary
from smtp import sent_summary as sent_summary

# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _since_to_ts(since: str | None) -> int | None:
    """Parse `--since` to a unix-seconds cutoff: `30d` / `12h` / ISO `2026-05-07`."""
    if since is None:
        return None
    m = re.fullmatch(r"(\d+)([dh])", since)
    if m:
        secs = int(m.group(1)) * (86400 if m.group(2) == "d" else 3600)
        return int(time.time()) - secs
    parsed = _dt.datetime.fromisoformat(since)
    parsed = parsed.replace(tzinfo=_dt.UTC) if parsed.tzinfo is None else parsed.astimezone(_dt.UTC)
    return int(parsed.timestamp())


def _body_arg(body: str | None) -> str:
    """The send/reply body: `--body` if given, else read it from stdin -- rejecting
    an empty/whitespace body from either source (a blank send is a mistake)."""
    if body is None:
        # str(...): sys.stdin.read()'s stub resolves to `str | Any`, and pyright
        # does not narrow a wider-declared-type variable via an `Any`-containing
        # assignment — the explicit str() coercion narrows `body` to `str` here.
        body = str(sys.stdin.read())
    if not body.strip():
        raise ValueError("no body: pass a non-empty --body or pipe text on stdin")
    return body


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gmail", description="search / read / enumerate Gmail over IMAP -> JSON"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("search", help="run a Gmail search, return metadata")
    ps.add_argument("--query", required=True, help="Gmail search syntax (X-GM-RAW)")
    ps.add_argument("--limit", type=int, default=50)

    pr = sub.add_parser("read", help="return matching messages' extracted text")
    pr.add_argument("--query", help="Gmail search syntax")
    pr.add_argument("--message-id", help="RFC822 Message-Id")
    pr.add_argument("--limit", type=int, default=5)

    pd = sub.add_parser("discover", help="list distinct newsletter List-Ids (seed S2)")
    pd.add_argument("--since", help="time bound: 30d / 12h / 2026-05-07")

    for name in ("enum", "sync"):
        sp = sub.add_parser(name, help=f"{name} one newsletter (List-Id)")
        sp.add_argument("--list-id", required=True, help="List-Id value, e.g. foo.substack.com")
        sp.add_argument("--since", help="time bound: 30d / 12h / 2026-05-07")
        sp.add_argument("--limit", type=int)

    pf = sub.add_parser("fetch", help="fetch one message into the raw mirror -> S1")
    pf.add_argument("--message-id", required=True, help="RFC822 Message-Id")

    psd = sub.add_parser("send", help="compose + send a new email (side-effectful)")
    psd.add_argument("--to", required=True, help="recipient(s), comma-separated")
    psd.add_argument("--subject", required=True)
    psd.add_argument("--body", help="body text (omit to read from stdin)")
    psd.add_argument("--cc", help="cc recipient(s), comma-separated")
    psd.add_argument("--attach", action="append", help="file path to attach (repeatable)")
    psd.add_argument("--dry-run", action="store_true", help="compose + print, do not send")

    prp = sub.add_parser("reply", help="reply to a message by Message-Id (side-effectful)")
    prp.add_argument("--message-id", required=True, help="RFC822 Message-Id to reply to")
    prp.add_argument("--body", help="body text (omit to read from stdin)")
    prp.add_argument("--reply-all", action="store_true", help="also Cc the original To+Cc")
    prp.add_argument("--attach", action="append", help="file path to attach (repeatable)")
    prp.add_argument("--dry-run", action="store_true", help="compose + print, do not send")

    pfw = sub.add_parser("forward", help="forward a message by Message-Id (side-effectful)")
    pfw.add_argument("--message-id", required=True, help="RFC822 Message-Id to forward")
    pfw.add_argument("--to", required=True, help="recipient(s), comma-separated")
    pfw.add_argument("--body", default="", help="optional note above the forwarded message")
    pfw.add_argument("--cc", help="cc recipient(s), comma-separated")
    pfw.add_argument("--attach", action="append", help="extra file path to attach (repeatable)")
    pfw.add_argument("--dry-run", action="store_true", help="compose + print, do not send")

    pdr = sub.add_parser("draft", help="compose into the Drafts folder (never sends)")
    pdr.add_argument("--to", required=True, help="recipient(s), comma-separated")
    pdr.add_argument("--subject", required=True)
    pdr.add_argument("--body", help="body text (omit to read from stdin)")
    pdr.add_argument("--cc", help="cc recipient(s), comma-separated")
    pdr.add_argument("--attach", action="append", help="file path to attach (repeatable)")
    pdr.add_argument("--dry-run", action="store_true", help="compose + print, do not save")

    pdd = sub.add_parser("draft-delete", help="delete a draft by its Message-Id")
    pdd.add_argument("--message-id", required=True, help="RFC822 Message-Id of the draft")

    return p


def _main(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    if args.cmd == "search":
        result: Any = search(args.query, limit=args.limit)
    elif args.cmd == "read":
        result = read(query=args.query, message_id=args.message_id, limit=args.limit)
    elif args.cmd == "discover":
        result = discover(since_ts=_since_to_ts(args.since))
    elif args.cmd == "enum":
        result = enum(args.list_id, since_ts=_since_to_ts(args.since), limit=args.limit)
    elif args.cmd == "sync":
        result = sync(args.list_id, since_ts=_since_to_ts(args.since), limit=args.limit)
    elif args.cmd == "fetch":
        result = to_s1(fetch(args.message_id))
    elif args.cmd == "send":
        result = send(
            args.to,
            args.subject,
            _body_arg(args.body),
            cc=args.cc,
            attach=args.attach,
            dry_run=args.dry_run,
        )
    elif args.cmd == "forward":
        result = forward(
            args.message_id,
            args.to,
            body=args.body,
            cc=args.cc,
            attach=args.attach,
            dry_run=args.dry_run,
        )
    elif args.cmd == "draft":
        result = draft(
            args.to,
            args.subject,
            _body_arg(args.body),
            cc=args.cc,
            attach=args.attach,
            dry_run=args.dry_run,
        )
    elif args.cmd == "draft-delete":
        result = draft_delete(args.message_id)
    else:
        result = reply(
            args.message_id,
            _body_arg(args.body),
            reply_all=args.reply_all,
            attach=args.attach,
            dry_run=args.dry_run,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
