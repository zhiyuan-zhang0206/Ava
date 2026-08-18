"""
Gmail write half: compose + send over SMTP, reply / forward, and drafts
(IMAP APPEND). Side-effectful - `send` / `reply` / `forward` put real
mail on the wire; `--dry-run` composes without sending. Split out of
feed.py (2026-08-07, Task #1011) so the CLI entry stays under the
800-line hard ceiling.
"""

from __future__ import annotations

import email.utils
import imaplib
import mimetypes
import re
import smtplib
import time
from email.message import EmailMessage, Message
from functools import cache as _cache
from pathlib import Path
from typing import Any

from _imap import (
    ALL_MAIL,
    GmailError,
    _account,
    _app_password,
    _body_text,
    _conn,
    _full,
    _search,
)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587  # STARTTLS; the App Password authenticates SMTP as well as IMAP
# --------------------------------------------------------------------------- #
# Write lens (SMTP) -- send / reply. Side-effectful.
# --------------------------------------------------------------------------- #


def _addr_list(raw: str | None) -> list[str]:
    """Parse a comma-separated address header into bare addresses (RFC 5322 uses
    commas; `getaddresses` does not split on `;`)."""
    if not raw:
        return []
    return [addr for _, addr in email.utils.getaddresses([raw]) if addr]


def _attach_files(msg: EmailMessage, paths: list[str]) -> None:
    """Attach each file path to a composed message (turns it multipart/mixed).

    maintype/subtype is guessed from the filename, falling back to
    application/octet-stream. A path that is not a readable file raises rather
    than sending a message with a silently-missing attachment."""
    for p in paths:
        path = Path(p).expanduser()
        if not path.is_file():
            raise ValueError(f"--attach path is not a readable file: {p}")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        maintype, _, subtype = ctype.partition("/")
        msg.add_attachment(
            path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name
        )


def _compose(
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    in_reply_to: str | None = None,
    references: str | None = None,
    attachments: list[str] | None = None,
) -> EmailMessage:
    """Build an EmailMessage (plain text, plus any file attachments) from the
    authenticated account."""
    acct = _account()
    msg = EmailMessage()
    msg["From"] = acct
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-Id"] = email.utils.make_msgid(domain=acct.rsplit("@", 1)[-1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    if attachments:
        _attach_files(msg, attachments)
    return msg


def _smtp_send(msg: EmailMessage) -> None:
    """Send a composed message via Gmail SMTP (STARTTLS + App Password)."""
    acct = _account()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.starttls()
        try:
            s.login(acct, _app_password())
        except smtplib.SMTPAuthenticationError as e:
            raise GmailError(
                f"SMTP login failed for {acct}: {e}. The App Password authenticates SMTP too; "
                "regenerate it if this persists."
            ) from e
        s.send_message(msg)


def _msg_summary(msg: EmailMessage) -> dict[str, Any]:
    # get_content() raises on a multipart (attachment) message, so read the body
    # via get_body, which returns the message itself when it is single-part.
    body_part = msg.get_body(preferencelist=("plain",))
    return {
        "message_id": (msg["Message-Id"] or "").strip().strip("<>") or None,
        "from": msg["From"],
        "to": msg["To"],
        "cc": msg["Cc"],
        "subject": msg["Subject"],
        "in_reply_to": (msg["In-Reply-To"] or "").strip().strip("<>") or None,
        "attachments": [a.get_filename() for a in msg.iter_attachments()],
        "body": (body_part.get_content() if body_part else "").strip(),
    }


def _sent_summary(msg: EmailMessage, *, sent: bool) -> dict[str, Any]:
    return {"sent": sent, **_msg_summary(msg)}


def send(
    to: str | list[str],
    subject: str,
    body: str,
    *,
    cc: str | list[str] | None = None,
    attach: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Compose and (unless `dry_run`) send a new email from the account.

    SIDE-EFFECTFUL: a real email leaves the mailbox. `attach` is a list of file
    paths to attach. `dry_run=True` builds and returns the composed message
    (headers + body + attachment names) WITHOUT touching SMTP, so the caller can
    inspect it before committing to a real send. `sent` is False under dry_run."""
    to_list = _addr_list(to) if isinstance(to, str) else list(to)
    if not to_list:
        raise ValueError("send needs at least one recipient")
    cc_list = _addr_list(cc) if isinstance(cc, str) else list(cc or [])
    msg = _compose(to=to_list, subject=subject, body=body, cc=cc_list or None, attachments=attach)
    if not dry_run:
        _smtp_send(msg)
    return _sent_summary(msg, sent=not dry_run)


def reply(
    message_id: str,
    body: str,
    *,
    reply_all: bool = False,
    attach: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Reply to an existing message (looked up by RFC822 Message-Id).

    Threads correctly (`In-Reply-To` + `References`), `Re:`-prefixes the subject,
    and addresses the original sender (`Reply-To` if set, else `From`);
    `reply_all` adds the original To+Cc minus our own address. `attach` is a list
    of file paths to attach. SIDE-EFFECTFUL unless `dry_run` (which composes from
    the fetched original without sending)."""
    mid = message_id.strip().strip("<>")
    ids = _search(f"rfc822msgid:{mid}")
    if not ids:
        raise GmailError(f"no message with Message-Id {message_id!r}")
    _, orig = _full(ids[-1])
    acct = _account().lower()

    to = _addr_list(orig["Reply-To"]) or _addr_list(orig["From"])
    if not to:
        raise GmailError(f"cannot reply to {message_id!r}: original has no Reply-To/From address")
    cc: list[str] = []
    if reply_all:
        seen = {a.lower() for a in to} | {acct}
        for addr in _addr_list(orig["To"]) + _addr_list(orig["Cc"]):
            if addr.lower() not in seen:
                cc.append(addr)
                seen.add(addr.lower())

    subject = (orig["Subject"] or "").strip()
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    orig_mid = (orig["Message-Id"] or "").strip()
    references = " ".join(p for p in [(orig["References"] or "").strip(), orig_mid] if p) or None

    msg = _compose(
        to=to,
        subject=subject,
        body=body,
        cc=cc or None,
        in_reply_to=orig_mid or None,
        references=references,
        attachments=attach,
    )
    if not dry_run:
        _smtp_send(msg)
    return _sent_summary(msg, sent=not dry_run)


def _carry_attachments(msg: EmailMessage, orig: Message) -> None:
    """Re-attach the original message's attachment parts onto a forward (real
    attachments and inline parts alike, as Gmail's own forward does)."""
    for i, part in enumerate(orig.iter_attachments()):
        payload = part.get_content()
        data = payload if isinstance(payload, (bytes, bytearray)) else str(payload).encode()
        maintype, _, subtype = part.get_content_type().partition("/")
        msg.add_attachment(
            data, maintype=maintype, subtype=subtype, filename=part.get_filename() or f"part-{i}"
        )


def forward(
    message_id: str,
    to: str | list[str],
    *,
    body: str = "",
    cc: str | list[str] | None = None,
    attach: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Forward an existing message (looked up by RFC822 Message-Id) to `to`.

    `Fwd:`-prefixes the subject and quotes the original (a Gmail-style header
    block + the extracted body text) below the optional `body` note; the
    original's attachments are carried along, and `attach` adds new ones.
    SIDE-EFFECTFUL unless `dry_run`."""
    mid = message_id.strip().strip("<>")
    ids = _search(f"rfc822msgid:{mid}")
    if not ids:
        raise GmailError(f"no message with Message-Id {message_id!r}")
    _, orig = _full(ids[-1])

    to_list = _addr_list(to) if isinstance(to, str) else list(to)
    if not to_list:
        raise ValueError("forward needs at least one recipient")
    cc_list = _addr_list(cc) if isinstance(cc, str) else list(cc or [])

    subject = (orig["Subject"] or "").strip()
    if not subject.lower().startswith("fwd:"):
        subject = f"Fwd: {subject}"
    quoted = "\n".join(
        [
            "---------- Forwarded message ----------",
            f"From: {(orig['From'] or '').strip()}",
            f"Date: {(orig['Date'] or '').strip()}",
            f"Subject: {(orig['Subject'] or '').strip()}",
            f"To: {(orig['To'] or '').strip()}",
            "",
            _body_text(orig),
        ]
    )
    full_body = (body.strip() + "\n\n" + quoted) if body.strip() else quoted

    msg = _compose(
        to=to_list, subject=subject, body=full_body, cc=cc_list or None, attachments=attach
    )
    _carry_attachments(msg, orig)
    if not dry_run:
        _smtp_send(msg)
    return _sent_summary(msg, sent=not dry_run)


# --------------------------------------------------------------------------- #
# Draft lens (IMAP APPEND) -- draft / draft-delete. Nothing is ever sent.
# --------------------------------------------------------------------------- #


@_cache
def _drafts_folder() -> str:
    """The mailbox's drafts folder name, discovered via its RFC 6154 `\\Drafts`
    special-use attribute (the display name is localized per account; the
    attribute is not). Raises when the server advertises no such folder."""
    typ, data = _conn().list()
    if typ != "OK":
        raise GmailError(f"LIST failed (typ={typ})")
    for line in data:
        s = line.decode() if isinstance(line, bytes) else str(line)
        m = re.match(r'\(([^)]*)\)\s+(?:"[^"]*"|NIL)\s+(.+)$', s)
        if not m:
            raise GmailError(f"unparseable LIST line: {s!r}")
        attrs, name = m.groups()
        if "\\drafts" in attrs.lower():
            name = name.strip()
            return name[1:-1] if name.startswith('"') and name.endswith('"') else name
    raise GmailError("the server advertises no \\Drafts special-use folder")


def draft(
    to: str | list[str],
    subject: str,
    body: str,
    *,
    cc: str | list[str] | None = None,
    attach: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Compose a message into the Drafts folder -- nothing is sent.

    The draft lands via IMAP APPEND with the `\\Draft` flag, so it shows up in
    the Drafts folder of every Gmail client, editable and sendable from there
    (the agent-drafts / human-sends flow). The returned `message_id` is the
    handle for `draft_delete`. `dry_run=True` composes without writing."""
    to_list = _addr_list(to) if isinstance(to, str) else list(to)
    if not to_list:
        raise ValueError("draft needs at least one recipient")
    cc_list = _addr_list(cc) if isinstance(cc, str) else list(cc or [])
    msg = _compose(to=to_list, subject=subject, body=body, cc=cc_list or None, attachments=attach)
    if not dry_run:
        folder = _drafts_folder()
        typ, data = _conn().append(
            f'"{folder}"', r"(\Draft)", imaplib.Time2Internaldate(time.time()), msg.as_bytes()
        )
        if typ != "OK":
            raise GmailError(f"APPEND to {folder!r} failed (typ={typ}): {data!r}")
    return {"drafted": not dry_run, **_msg_summary(msg)}


def draft_delete(message_id: str) -> dict[str, Any]:
    """Delete a draft from the Drafts folder by its RFC822 Message-Id.

    Selects the Drafts folder read-write (the only write-mode select in this
    module), flags every copy `\\Deleted`, expunges, then re-selects All Mail
    read-only to restore the connection's usual state. Raises when no draft
    carries that Message-Id."""
    mid = message_id.strip().strip("<>")
    folder = _drafts_folder()
    conn = _conn()
    typ, _ = conn.select(f'"{folder}"', readonly=False)
    if typ != "OK":
        raise GmailError(f"cannot select {folder!r} read-write (typ={typ})")
    try:
        ids = _search(f"rfc822msgid:{mid}")
        if not ids:
            raise GmailError(f"no draft with Message-Id {message_id!r} in {folder!r}")
        for uid in ids:
            typ, data = conn.store(uid.decode(), "+FLAGS", r"(\Deleted)")
            if typ != "OK":
                raise GmailError(f"STORE \\Deleted on {uid!r} failed (typ={typ}): {data!r}")
        conn.expunge()
    finally:
        conn.select(ALL_MAIL, readonly=True)
    return {"deleted": len(ids), "message_id": mid, "folder": folder}
