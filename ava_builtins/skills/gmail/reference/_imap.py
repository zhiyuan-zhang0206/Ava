"""
Gmail ingest half: Keychain auth, IMAP connection/search/fetch, content
extraction, and the feed lenses (search / read / discover / enum / fetch /
save / sync). Split out of feed.py (2026-08-07, Task #1011) so the CLI
entry stays under the 800-line hard ceiling.
"""

from __future__ import annotations

import datetime as _dt
import email.utils
import hashlib
import html
import imaplib
import json
import mimetypes
import re
import subprocess
from email.message import Message
from email.parser import BytesParser
from email.policy import default as _default_policy
from functools import cache as _cache
from pathlib import Path
from typing import Any

from loguru import logger

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587  # STARTTLS; the App Password authenticates SMTP as well as IMAP
KEYCHAIN_SERVICE = "ava-gmail-imap"
# Gmail's "All Mail" holds archived issues too, not just the inbox -- a newsletter
# the user archived is still a feed item, so enum/search run against All Mail.
ALL_MAIL = '"[Gmail]/All Mail"'
MIRROR_ROOT = Path("~/Downloads/gmail").expanduser()

# Coarse discovery net (recall-broad, precision-loose): Gmail's own bulk tabs.
# `discover` widens to these then keeps only List-Id-bearing messages, because
# Gmail exposes no server-side "has a List-Id header" filter (a bare `list:*`
# wildcard does not filter, and IMAP `HEADER List-Id ""` is rejected as
# infeasible) -- the List-Id confirm is always client-side after the fetch.
DISCOVER_NET = "category:updates OR category:forums"

# Substack and most editorial senders prefix the plaintext body with this line;
# it carries the canonical post permalink (the S1 `url`).
_RE_VIEW_ONLINE = re.compile(r"View this post on the web at (\S+)", re.IGNORECASE)
_RE_TAG = re.compile(r"<[^>]+>")
_RE_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_RE_WS = re.compile(r"[ \t]*\n[ \t]*")


class GmailError(Exception):
    """A Gmail IMAP operation failed: Keychain unreadable, login rejected,
    a search Gmail refused, or a response whose shape we did not expect."""


# --------------------------------------------------------------------------- #
# Keychain -> mailbox identity + secret
# --------------------------------------------------------------------------- #


def _keychain(*extra: str) -> str:
    """Read the `ava-gmail-imap` generic-password entry; `extra` selects the field.

    No `extra` returns the attribute dump (to parse the account label); `("-w",)`
    returns the password. A missing entry / denied Keychain raises `GmailError`
    with the cause rather than returning empty."""
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, *extra],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        raise GmailError(
            f"could not read Keychain entry (service={KEYCHAIN_SERVICE!r}): "
            f"security command not found (not macOS). Create it with: "
            f"security add-generic-password -a <you@gmail.com> -s {KEYCHAIN_SERVICE} -w"
        ) from None
    if proc.returncode != 0:
        raise GmailError(
            f"could not read Keychain entry (service={KEYCHAIN_SERVICE!r}): "
            f"{proc.stderr.strip() or 'not found'}. Create it with: "
            f"security add-generic-password -a <you@gmail.com> -s {KEYCHAIN_SERVICE} -w"
        )
    return proc.stdout


@_cache
def _account() -> str:
    """The Gmail address to log in as -- the `acct` label of the Keychain entry."""
    m = re.search(r'"acct"<blob>="([^"]+)"', _keychain())
    if not m:
        raise GmailError(f"Keychain entry {KEYCHAIN_SERVICE!r} has no account (acct) attribute")
    return m.group(1)


def _app_password() -> str:
    # Gmail accepts the 16-char app password with or without its display spaces;
    # strip them so a copy-paste that kept the "abcd efgh ..." grouping still works.
    #
    # Reads from the macOS Keychain first; when that is unavailable (e.g. a
    # headless agent without GUI access), falls back to a plain-text file at
    # ~/.ava/secrets/gmail-app-password.
    try:
        return _keychain("-w").strip().replace(" ", "")
    except GmailError:
        logger.debug("Gmail app password not found in macOS Keychain, falling back to secrets file")
    secrets_file = Path("~/.ava/secrets/gmail-app-password").expanduser()
    if secrets_file.exists():
        try:
            pwd = secrets_file.read_text().strip().replace(" ", "")
            if pwd:
                return pwd
        except OSError as e:
            logger.warning("Failed to read Gmail secrets file at {}: {}", secrets_file, e)
    raise GmailError(
        f"could not read Keychain entry (service={KEYCHAIN_SERVICE!r}) "
        f"and no secrets file at {secrets_file}. Create it with: "
        f"security add-generic-password -a <you@gmail.com> -s {KEYCHAIN_SERVICE} -w"
    )


# --------------------------------------------------------------------------- #
# IMAP connection + search/fetch plumbing
# --------------------------------------------------------------------------- #


@_cache
def _conn() -> imaplib.IMAP4_SSL:
    """A logged-in connection, selected read-only on All Mail (cached per run)."""
    user = _account()
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        conn.login(user, _app_password())
    except imaplib.IMAP4.error as e:
        raise GmailError(
            f"IMAP login failed for {user}: {e}. Check the app password is current and "
            "IMAP is enabled (Gmail settings -> Forwarding and POP/IMAP -> Enable IMAP)."
        ) from e
    typ, _ = conn.select(ALL_MAIL, readonly=True)
    if typ != "OK":
        raise GmailError(f"cannot select {ALL_MAIL} (typ={typ})")
    return conn


def _xgm(query: str) -> str:
    """Quote a Gmail-syntax query as an IMAP string for the X-GM-RAW criterion."""
    return '"' + query.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _search(query: str) -> list[bytes]:
    """Gmail-search via X-GM-RAW; return matching sequence numbers (oldest-first).

    Gmail signals a rejected query by answering the SEARCH with `[SERVERBUG]
    infeasible query` in the data line rather than a BAD status, so a non-OK
    status or an empty-but-error payload both raise instead of looking like a
    zero-result match."""
    typ, data = _conn().search(None, "X-GM-RAW", _xgm(query))
    if typ != "OK":
        raise GmailError(f"search failed (typ={typ}) for {query!r}: {data!r}")
    payload = data[0] if data else b""
    if payload and payload.split()[0].startswith(b"["):
        raise GmailError(f"Gmail refused the search {query!r}: {payload.decode(errors='replace')}")
    return payload.split() if payload else []


def _recent(ids: list[bytes], n: int | None) -> list[bytes]:
    """The newest `n` ids, newest-first (IMAP returns ascending sequence order).

    A negative `n` is a caller bug (it would slice off the *oldest* `|n|` and
    silently return a wrong-length list), so reject it rather than mis-slice."""
    if n is not None and n < 0:
        raise ValueError(f"limit/scan must be >= 0, got {n}")
    newest_first = ids[::-1]
    return newest_first[:n] if n is not None else newest_first


def _fetch(uid: bytes, parts: str) -> bytes:
    typ, data = _conn().fetch(uid.decode(), parts)
    if typ != "OK":
        raise GmailError(f"fetch {uid!r} {parts} failed: typ={typ}")
    raw = next((p[1] for p in data if isinstance(p, tuple)), None)
    if raw is None:
        raise GmailError(f"fetch {uid!r} {parts} returned no literal")
    return raw


def _headers(uid: bytes) -> Message:
    return BytesParser(policy=_default_policy).parsebytes(_fetch(uid, "(BODY.PEEK[HEADER])"))


def _full(uid: bytes) -> tuple[bytes, Message]:
    raw = _fetch(uid, "(RFC822)")
    return raw, BytesParser(policy=_default_policy).parsebytes(raw)


# --------------------------------------------------------------------------- #
# Header extraction
# --------------------------------------------------------------------------- #


def _parse_list_id(raw: str | None) -> str | None:
    """The List-Id value: the `<...>` bracket content (`Name <id.domain>` or bare
    `<id.domain>`), else the trimmed string. None when the header is absent."""
    if not raw:
        return None
    # RFC 2919: `[description] <list-id>` -- the id is the LAST bracket pair (the
    # optional human description precedes it and may itself contain `<...>`), and
    # the id is a dot-atom that never contains brackets, so the last pair is it.
    groups = re.findall(r"<([^>]+)>", raw)
    return groups[-1].strip() if groups else raw.strip()


def _iso(date_hdr: str | None) -> str | None:
    if not date_hdr:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(date_hdr)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.UTC)
    return dt.astimezone(_dt.UTC).isoformat().replace("+00:00", "Z")


def _now_iso() -> str:
    return _dt.datetime.now(tz=_dt.UTC).isoformat().replace("+00:00", "Z")


def _meta(msg: Message) -> dict[str, Any]:
    """The metadata every lens returns -- newsletter identity lives in List-Id."""
    return {
        "message_id": (msg["Message-Id"] or "").strip().strip("<>") or None,
        "list_id": _parse_list_id(msg["List-Id"]),
        "from": (msg["From"] or "").strip() or None,
        "subject": (msg["Subject"] or "").strip() or None,
        "date": _iso(msg["Date"]),
    }


# --------------------------------------------------------------------------- #
# Content extraction (text/plain preferred, html stripped as fallback)
# --------------------------------------------------------------------------- #


def _html_to_text(html_body: str) -> str:
    no_scripts = _RE_SCRIPT_STYLE.sub(" ", html_body)
    text = html.unescape(_RE_TAG.sub(" ", no_scripts))
    return _RE_WS.sub("\n", re.sub(r"[ \t]{2,}", " ", text)).strip()


def _body_text(msg: Message) -> str:
    """The issue body as text: the text/plain MIME part when present (already
    close to clean markdown for Substack/Ghost/Buttondown senders), else the
    text/html part stripped to text. Newsletters are heavily-templated MIME, so
    plain-text-first skips most chrome for free."""
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        return str(plain.get_content()).strip()
    htmlpart = msg.get_body(preferencelist=("html",))
    if htmlpart is not None:
        return _html_to_text(str(htmlpart.get_content()))
    return ""


def _canonical_url(text: str) -> str | None:
    m = _RE_VIEW_ONLINE.search(text)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# Lenses: search / read
# --------------------------------------------------------------------------- #


def search(query: str, *, limit: int | None = 50) -> list[dict[str, Any]]:
    """Run a Gmail search (full `X-GM-RAW` syntax), newest-first, return metadata.

    The general "find me mail" lens -- e.g. `from:foo newer_than:7d`,
    `subject:invoice`, `category:updates`. Each item is `_meta` (message_id /
    list_id / from / subject / date)."""
    return [_meta(_headers(uid)) for uid in _recent(_search(query), limit)]


def read(
    *, query: str | None = None, message_id: str | None = None, limit: int = 5
) -> list[dict[str, Any]]:
    """Return matching messages' extracted text (headers + body), newest-first.

    Exactly one selector: `query` (a Gmail search) or `message_id` (an RFC822
    Message-Id, looked up via `rfc822msgid:`). The read lens for "open this mail"
    -- not the feed mirror (that is `fetch`)."""
    if (query is None) == (message_id is None):
        raise ValueError("read needs exactly one of query / message_id")
    q = query if query is not None else f"rfc822msgid:{message_id.strip().strip('<>')}"
    out: list[dict[str, Any]] = []
    for uid in _recent(_search(q), limit):
        _, msg = _full(uid)
        out.append({**_meta(msg), "text": _body_text(msg)})
    return out


# --------------------------------------------------------------------------- #
# Lenses: discover / enum (feed) -- a newsletter is a List-Id
# --------------------------------------------------------------------------- #


def _after(since_ts: int | None) -> str:
    """`after:<epoch>` clause for a unix-seconds cutoff (Gmail accepts epoch)."""
    return f" after:{since_ts}" if since_ts is not None else ""


def discover(*, since_ts: int | None = None, max_scan: int = 400) -> list[dict[str, Any]]:
    """List the distinct newsletter List-Ids seen in the discovery net, to seed S2.

    Walks `category:updates|forums` over the window, keeps List-Id-bearing
    messages, and aggregates by List-Id: `{list_id, count, sample_from,
    sample_subject}` sorted by count. The user registers the ones they want in
    the memory follow-note; the skill itself stores no follow list."""
    agg: dict[str, dict[str, Any]] = {}
    # Parenthesize the disjunction so `after:` binds the whole net -- unparenthesized,
    # Gmail's `OR` binds tighter than the implicit space-AND, leaving the `updates`
    # arm unbounded by --since.
    for uid in _recent(_search(f"({DISCOVER_NET}){_after(since_ts)}"), max_scan):
        m = _meta(_headers(uid))
        lid = m["list_id"]
        if not lid:
            continue
        row = agg.setdefault(
            lid,
            {"list_id": lid, "count": 0, "sample_from": m["from"], "sample_subject": m["subject"]},
        )
        row["count"] += 1
    return sorted(agg.values(), key=lambda r: r["count"], reverse=True)


def enum(
    list_id: str, *, since_ts: int | None = None, limit: int | None = None, max_scan: int = 200
) -> list[dict[str, Any]]:
    """List one newsletter's new issues newest-first (`list:<list_id> after:...`).

    `list_id` is the List-Id value (e.g. `newsletter.example.com`) -- the
    S2 follow key. `list:` filters server-side, so every result already belongs to
    this newsletter; bounded by `--since` (server-side) and `--limit`/`max_scan`.
    Each item is `_meta`."""
    if limit is not None and limit <= 0:
        return []
    ids = _search(f"list:{list_id}{_after(since_ts)}")
    return [_meta(_headers(uid)) for uid in _recent(ids, limit if limit is not None else max_scan)]


# --------------------------------------------------------------------------- #
# Per-item fetch -> raw mirror -> S1
# --------------------------------------------------------------------------- #


def _safe_id(message_id: str) -> str:
    """A filesystem-safe mirror dir name derived from the RFC822 Message-Id."""
    core = message_id.strip().strip("<>")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", core)
    # Empty, or a pure dot-segment (`.` / `..`): both would resolve to the mirror
    # root or its parent (a Message-Id is attacker-set), so hash it instead.
    if not safe or set(safe) <= {"."}:
        return hashlib.sha256(message_id.encode()).hexdigest()[:16]
    if len(safe) > 120:
        return safe[:80] + "-" + hashlib.sha256(core.encode()).hexdigest()[:12]
    return safe


def _outdir(message_id: str, base: Path) -> Path:
    """The mirror dir for a message, asserting it stays inside `base` (the
    Message-Id is sender-set, so containment is checked, not trusted)."""
    outdir = base / _safe_id(message_id)
    if not outdir.resolve().is_relative_to(base.resolve()):
        raise ValueError(f"refusing to write outside the mirror root: {outdir}")
    return outdir


def _safe_filename(raw: str | None, *, fallback: str, content_type: str) -> str:
    """A single-component, traversal-safe name for an attachment. The sender sets
    the filename, so drop directory parts + unsafe chars and refuse leading dots
    (no `.`/`..`/hidden); a nameless part is named from `fallback` + a guessed ext."""
    base = (raw or "").replace("\\", "/").split("/")[-1].strip()
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".")
    if not base:
        base = fallback + (mimetypes.guess_extension(content_type) or ".bin")
    return base[:120]


def _attachments(msg: Message, outdir: Path) -> list[dict[str, Any]]:
    """Write every non-body part to `<outdir>/attachments/`, return the manifest.

    Real attachments and inline parts (e.g. a signature logo) alike are saved --
    `disposition` ('attachment'/'inline') tells them apart -- each to a
    deduplicated, traversal-safe filename. A row is `{filename, content_type,
    disposition, size, path}`. No parts means an empty list and no dir created."""
    rows: list[dict[str, Any]] = []
    used: set[str] = set()
    adir = outdir / "attachments"
    for i, part in enumerate(msg.iter_attachments()):
        ctype = part.get_content_type()
        name = base = _safe_filename(
            part.get_filename(), fallback=f"attachment-{i}", content_type=ctype
        )
        n = 1
        while name in used:  # collision within one message: suffix the stem
            name = f"{Path(base).stem}_{n}{Path(base).suffix}"
            n += 1
        used.add(name)
        payload = part.get_content()
        data = payload if isinstance(payload, (bytes, bytearray)) else str(payload).encode()
        adir.mkdir(parents=True, exist_ok=True)
        (adir / name).write_bytes(data)
        rows.append(
            {
                "filename": name,
                "content_type": ctype,
                "disposition": part.get_content_disposition(),
                "size": len(data),
                "path": str(adir / name),
            }
        )
    return rows


def fetch(message_id: str, *, root: Path | None = None) -> dict[str, Any]:
    """Fetch one message into the raw mirror `~/Downloads/gmail/<safe-id>/`.

    Looks the message up by its RFC822 Message-Id (`rfc822msgid:`), writes
    `raw.eml` (never-deleted raw) + `post.json` + `post.md` + any attachments
    under `attachments/`, and returns the curated post dict (with an
    `attachments` manifest). Body text is the plain part (or stripped html)."""
    message_id = message_id.strip().strip("<>")  # CLI passes <id>, sync passes bare; canonicalize
    ids = _search(f"rfc822msgid:{message_id}")
    if not ids:
        raise GmailError(f"no message with Message-Id {message_id!r}")
    raw, msg = _full(ids[-1])
    meta = _meta(msg)
    if not meta["message_id"]:
        raise GmailError(f"message {message_id!r} has no Message-Id header")
    text = _body_text(msg)
    from_name = email.utils.parseaddr(meta["from"] or "")[0] or meta["from"]
    outdir = _outdir(meta["message_id"], root or MIRROR_ROOT)
    outdir.mkdir(parents=True, exist_ok=True)
    post: dict[str, Any] = {
        "platform": "gmail",
        "message_id": meta["message_id"],
        "list_id": meta["list_id"],
        "url": _canonical_url(text),
        "title": meta["subject"],
        "text": text,
        "author": {"id": meta["list_id"], "name": from_name},
        "published_at": meta["date"],
        "list_unsubscribe": (msg["List-Unsubscribe"] or "").strip() or None,
        "attachments": _attachments(msg, outdir),
        "fetched_at": _now_iso(),
        "fetched_via": "gmail",
    }
    save(post, raw, root=root)
    return post


def save(post: dict[str, Any], raw_eml: bytes, *, root: Path | None = None) -> Path:
    """Persist a fetched message to `~/Downloads/gmail/<safe-id>/` (raw.eml +
    post.json + post.md; any attachments are written separately by `fetch`)."""
    if not post.get("message_id"):
        raise ValueError("refusing to save a message with no Message-Id")
    outdir = _outdir(post["message_id"], root or MIRROR_ROOT)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "raw.eml").write_bytes(raw_eml)
    (outdir / "post.json").write_text(json.dumps(post, ensure_ascii=False, indent=2))
    (outdir / "post.md").write_text(_render_markdown(post))
    return outdir


def _render_markdown(post: dict[str, Any]) -> str:
    a = post["author"]
    lines = [
        f"# {post.get('title') or post['message_id']}",
        "",
        f"- **from**: {a.get('name')} (list-id {a.get('id')})",
        f"- **url**: {post.get('url') or '_(no web permalink)_'}",
        f"- **published**: {post.get('published_at')}",
        "",
        post.get("text") or "_(empty body)_",
    ]
    return "\n".join(lines) + "\n"


def to_s1(post: dict[str, Any], *, matrix_entity: str | None = None) -> dict[str, Any]:
    """Project a fetched message onto the cross-platform S1 item schema shared
    with the `web-sources` adapters (see `web-sources/SKILL.md`). author.id =
    List-Id (the durable per-newsletter key); newsletters are singletons, so
    matrix_entity stays null."""
    a = post["author"]
    # S1 `media` is image|video only; other attachments (pdf, ...) still land on
    # disk + in post.json `attachments`, they just are not "media" in S1 terms.
    media = [
        {"kind": att["content_type"].split("/", 1)[0], "path": att["path"]}
        for att in post.get("attachments", [])
        if att["content_type"].split("/", 1)[0] in ("image", "video")
    ]
    return {
        "platform": "gmail",
        "source_id": post["message_id"],
        "url": post.get("url"),
        "author": {"id": a.get("id"), "name": a.get("name")},
        "matrix_entity": matrix_entity,
        "title": post.get("title"),
        "text": post.get("text"),
        "transcript": None,
        "published_at": post.get("published_at"),
        "fetched_at": post.get("fetched_at"),
        "fetched_via": post.get("fetched_via"),
        "media": media,
        "stats": {},
        "raw_path": str(MIRROR_ROOT / _safe_id(post["message_id"])) + "/",
    }


def _load_if_mirrored(message_id: str, *, root: Path | None) -> dict[str, Any] | None:
    """Return the saved post if its raw mirror already exists (re-runs over the
    same `--since` window reuse it instead of re-fetching) -- Message-Id is the
    stable, account-independent dedup key."""
    post_json = (root or MIRROR_ROOT) / _safe_id(message_id) / "post.json"
    if not post_json.exists():
        return None
    return json.loads(post_json.read_text())


def sync(
    list_id: str,
    *,
    since_ts: int | None = None,
    limit: int | None = None,
    matrix_entity: str | None = None,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """Enumerate a newsletter's new issues, fetch each, return the S1 items.

    Issues already in the raw mirror are reused (Message-Id dedup), so re-running
    the same window each digest is naturally idempotent."""
    out: list[dict[str, Any]] = []
    for it in enum(list_id, since_ts=since_ts, limit=limit):
        mid = it["message_id"]
        if not mid:
            continue
        post = _load_if_mirrored(mid, root=root) or fetch(mid, root=root)
        out.append(to_s1(post, matrix_entity=matrix_entity))
    return out
