"""IM Bridge Weixin adapter — Tencent iLink personal-bot API (WeChat official personal bot).

Long-polls ``ilink/bot/getupdates`` (35s window, continuation token persisted
under ``$AVA_HOME/state/im_bridge``) for inbound, ``ilink/bot/sendmessage``
(2000-char segmentation) for outbound. Every outbound message echoes the
peer's cached ``context_token`` (disk-backed), which iLink requires for session
continuity. Credentials come from the QR login CLI
(``python -m services.im_bridge.adapters.weixin --login``) and persist to
``$AVA_HOME/state/im_bridge/weixin_account.json`` (chmod 600). Unconfigured,
``start()`` logs its "unconfigured, skip" message and returns — the daemon keeps serving
other channels. Error hygiene: nothing from an httpx exception (which can
embed the request URL) ever reaches a caller, so a traceback cannot leak the
bot token.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import secrets
import struct
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from services.im_bridge.types import IMAdapter, InboundMessage
from shared.config import settings
from shared.log import logger

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"

# How long a failed send's client_id stays reusable for the retry. The
# retry is immediate (push_watchdog.send_with_retry), so 5 min is a wide
# margin; the expiry exists so a stale id can never be reused for a later,
# different message (iLink dedups by client_id and would drop it).
_PENDING_CLIENT_ID_TTL_S = 300.0
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = str((2 << 16) | (2 << 8) | 0)

LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_SECONDS = 15.0
QR_POLL_TIMEOUT_SECONDS = 35.0

_MAX_MESSAGE_LENGTH = 2000  # Weixin per-message cap for plain text

# iLink's context_token (carried by each user message) expires after a short,
# undetermined window — outbound pushes then fail with ret=-2 while getUpdates
# keeps working. Failure-based alerting lives in core._send /
# services/im_bridge/push_watchdog.py (Task #829).
_SEND_CHUNK_DELAY_SECONDS = 1.0  # polite pause between segments of one message

SESSION_EXPIRED_ERRCODE = -14
RATE_LIMIT_ERRCODE = -2
MAX_CONSECUTIVE_FAILURES = 3
RETRY_DELAY_SECONDS = 2.0
BACKOFF_DELAY_SECONDS = 30.0
SESSION_EXPIRED_PAUSE_SECONDS = 600.0
DEDUP_TTL_SECONDS = 300.0

ITEM_TEXT = 1
ITEM_VOICE = 3
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2


class _SessionExpiredError(Exception):
    """iLink session is stale (errcode -14, or -2 with "unknown error")."""


def _state_dir() -> Path:
    """``$AVA_HOME/state/im_bridge`` — shared state root for every adapter."""
    path = Path(settings.general.ava_home) / "state" / "im_bridge"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically (tmp file + rename) so readers never see a partial file;
    tmp chmod 0600 pre-rename → every output (tokens, buffers, account) is owner-only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    tmp.chmod(0o600)
    tmp.replace(path)


def load_account() -> dict[str, Any] | None:
    """Read the saved iLink account; None when unconfigured or corrupt."""
    path = _state_dir() / "weixin_account.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("weixin: failed to read account file {}: {}", path, exc)
        return None
    if not str(data.get("bot_token") or "").strip():
        return None
    return data


def save_account(*, account_id: str, bot_token: str, user_id: str, base_url: str) -> None:
    """Persist QR-login credentials atomically, owner-only (0600 — via
    `_atomic_write_json`, which every credential-bearing write shares)."""
    _atomic_write_json(
        _state_dir() / "weixin_account.json",
        {
            "account_id": account_id,
            "bot_token": bot_token,
            "user_id": user_id,
            "base_url": base_url,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )


class ContextTokenStore:
    """Disk-backed per-peer ``context_token`` cache (iLink session continuity)."""

    def __init__(self) -> None:
        self._path = _state_dir() / "weixin_context_tokens.json"
        self._cache: dict[str, str] = {}

    def restore(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("weixin: failed to restore context tokens: {}", exc)
            return
        for peer_id, token in data.items():
            if isinstance(token, str) and token:
                self._cache[peer_id] = token

    def get(self, peer_id: str) -> str | None:
        return self._cache.get(peer_id)

    def set(self, peer_id: str, token: str) -> None:
        self._cache[peer_id] = token
        self._persist()

    def drop(self, peer_id: str) -> None:
        """Forget a peer's token (called when iLink reports a stale session)."""
        if self._cache.pop(peer_id, None) is not None:
            self._persist()

    def _persist(self) -> None:
        try:
            _atomic_write_json(self._path, self._cache)
        except OSError as exc:
            logger.warning("weixin: failed to persist context tokens: {}", exc)


def _load_sync_buf() -> str:
    """The persisted getUpdates continuation token ("" on first run)."""
    path = _state_dir() / "weixin_sync.json"
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("get_updates_buf") or "")


def _save_sync_buf(sync_buf: str) -> None:
    try:
        _atomic_write_json(_state_dir() / "weixin_sync.json", {"get_updates_buf": sync_buf})
    except OSError as exc:
        logger.warning("weixin: failed to persist sync buf: {}", exc)


def _random_wechat_uin() -> str:
    """A random per-request X-WECHAT-UIN value (base64 of a uint32)."""
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _headers(token: str) -> dict[str, str]:
    """Headers for authenticated iLink POST endpoints."""
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {token}",
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }


def _get_headers() -> dict[str, str]:
    """Headers for iLink GET endpoints (the QR flow is pre-auth)."""
    return {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }


def _base_info() -> dict[str, str]:
    return {"channel_version": CHANNEL_VERSION}


def _is_stale_session_ret(ret: int | None, errcode: int | None, errmsg: Any) -> bool:
    """ret=-2/errcode=-2 with a stale-session errmsg is a stale session, not rate limit.

    iLink reuses ret=-2 for both conditions and only distinguishes them by
    errmsg: a stale/expired context_token (the common case for an outbound
    push after a long idle) comes back as "unknown error", "prepare failed",
    or an empty errmsg, while a genuine rate limit carries a populated
    message (e.g. "frequency limit"). Treating the ambiguous forms as stale
    lets _send_chunk retry once without the context_token, which iLink
    accepts as a degraded push (the QR-logged owner chat still works).
    """
    if RATE_LIMIT_ERRCODE not in (ret, errcode):
        return False
    msg = str(errmsg or "").strip().lower()
    if not msg:
        return True
    return msg in ("unknown error", "prepare failed")


def _extract_text(item_list: list[dict[str, Any]]) -> str:
    """First text item; falls back to Weixin's own voice transcription."""
    for item in item_list:
        if item.get("type") == ITEM_TEXT:
            return _get_str(item.get("text_item"), "text")
    for item in item_list:
        if item.get("type") == ITEM_VOICE:
            voice_text = _get_str(item.get("voice_item"), "text")
            if voice_text:
                return f"[voice transcript] {voice_text}"
    return ""


def _split_text(text: str, limit: int = _MAX_MESSAGE_LENGTH) -> list[str]:
    """Split ``text`` into at most ``limit``-char chunks, breaking at newlines."""
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, limit + 1)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


def _content_key(sender_id: str, text: str) -> str:
    return f"content:{sender_id}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _outbound_message(
    to: str, text: str, context_token: str | None, client_id: str
) -> dict[str, Any]:
    """One sendmessage ``msg`` payload; context_token is echoed when known.

    ``client_id`` is the idempotency key iLink dedups on — the caller owns
    it so a retry of a timed-out send reuses the SAME id (audit round 2,
    P1: a fresh uuid per attempt made the retry a duplicate send)."""
    message: dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": to,
        "client_id": client_id,
        "message_type": MSG_TYPE_BOT,
        "message_state": MSG_STATE_FINISH,
        "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
    }
    if context_token:
        message["context_token"] = context_token
    return {"msg": message}


def _safe_id(value: str, keep: int = 8) -> str:
    """Truncate long account/peer ids for log lines."""
    return value if len(value) <= keep else f"{value[:keep]}…"


def _get_str(data: dict[str, Any] | None, key: str) -> str:
    """A string field from a possibly-missing dict ("" when absent)."""
    if data is None:
        return ""
    return str(data.get(key) or "")


class WeixinAdapter(IMAdapter):
    """iLink personal-bot adapter — DM-only, text-only, QR-login credentials.

    Without an account file, ``start()`` logs the "unconfigured, skip" message and
    returns; ``send()`` raises (the core logs it, the daemon keeps serving others).
    """

    channel = "weixin"
    MAX_MESSAGE_LENGTH = _MAX_MESSAGE_LENGTH

    def __init__(self, core: Any, *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(core)
        self._client = client  # injected in tests; else created lazily
        self._owns_client = client is None
        self._poll_task: asyncio.Task[None] | None = None
        self._running = False
        self._tokens = ContextTokenStore()
        self._seen: dict[str, float] = {}  # dedup key -> monotonic timestamp
        self._last_inbound: dict[str, float] = {}  # peer -> epoch of last inbound msg
        # Push-failure watchdog state (Task #829): consecutive sendmessage
        # failures mean the iLink context_token expired — the core alerts the
        # user through other channels and hints "recovered" after the first
        # success (a fresh user message brought a new token).
        self.push_failures = 0
        self.push_failed_at: float | None = None
        self.push_recovered_at: float | None = None
        self._push_alerted_at: float | None = None
        self._chunk_delay_seconds = _SEND_CHUNK_DELAY_SECONDS
        # (chat_id, chunk_idx) -> (client_id, monotonic): the id of the last
        # failed send attempt, reused by the immediate retry so iLink's
        # client_id dedup collapses the duplicate (audit round 2, P1).
        # Entries expire so a stale id can never swallow a later, different
        # message.
        self._pending_client_ids: dict[tuple[str, int], tuple[str, float]] = {}
        account = load_account()
        self._account = account
        self._configured = account is not None
        self._account_id = str((account or {}).get("account_id") or "")
        self._base_url = str((account or {}).get("base_url") or ILINK_BASE_URL).rstrip("/")
        self._token = str((account or {}).get("bot_token") or "")
        self._user_id = str((account or {}).get("user_id") or "")

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(API_TIMEOUT_SECONDS))
            self._owns_client = True
        return self._client

    async def start(self) -> None:
        """Connect and start the long-poll loop; skip when unconfigured."""
        if not self._configured:
            logger.info("weixin not configured, skipping")
            return
        self._tokens.restore()
        self._restore_activity()
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop(), name="weixin-poll")
        logger.info(
            "weixin connected account={} base={}",
            _safe_id(self._account_id),
            self._base_url,
        )

    async def stop(self) -> None:
        """Stop the poll loop and release the HTTP client."""
        self._running = False
        task, self._poll_task = self._poll_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
            self._owns_client = False

    async def _poll_loop(self) -> None:
        """Long-poll getUpdates forever; reconnect with backoff on failure."""
        sync_buf = _load_sync_buf()
        timeout_ms = LONG_POLL_TIMEOUT_MS
        failures = 0
        while self._running:
            try:
                sync_buf, timeout_ms = await self._poll_once(sync_buf, timeout_ms)
                failures = 0
            except _SessionExpiredError:
                logger.warning("weixin session expired; pausing {}s", SESSION_EXPIRED_PAUSE_SECONDS)
                await asyncio.sleep(SESSION_EXPIRED_PAUSE_SECONDS)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                delay = (
                    BACKOFF_DELAY_SECONDS
                    if failures >= MAX_CONSECUTIVE_FAILURES
                    else RETRY_DELAY_SECONDS
                )
                logger.warning(
                    "weixin poll failed ({}/{}), retrying in {}s: {}",
                    failures,
                    MAX_CONSECUTIVE_FAILURES,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    failures = 0
            await asyncio.sleep(0)  # yield so instant polls never starve other tasks

    # -- inbound activity ------------------------------------------------------

    def _restore_activity(self) -> None:
        """Load persisted per-peer last-inbound timestamps (daemon restarts
        keep the peer map)."""
        path = _state_dir() / "weixin_activity.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("weixin: failed to restore activity: {}", exc)
            return
        raw_inbound: dict[str, Any] = data.get("last_inbound") or {}
        for peer_id, ts in raw_inbound.items():
            if isinstance(ts, (int, float)):
                self._last_inbound[str(peer_id)] = float(ts)

    def _persist_activity(self) -> None:
        """Persist per-peer activity (daemon restarts keep the window)."""
        try:
            _atomic_write_json(
                _state_dir() / "weixin_activity.json",
                {"last_inbound": self._last_inbound},
            )
        except OSError as exc:
            logger.warning("weixin: failed to persist activity: {}", exc)

    def _mark_inbound(self, peer_id: str) -> None:
        """Record inbound activity (each user message carries a fresh
        context_token, restoring outbound pushes)."""
        self._last_inbound[peer_id] = time.time()
        self._persist_activity()

    async def _poll_once(self, sync_buf: str, timeout_ms: int) -> tuple[str, int]:
        """One getUpdates round; returns the (possibly advanced) sync buffer and
        the server-suggested long-poll timeout."""
        resp = await self._post(
            EP_GET_UPDATES,
            {"get_updates_buf": sync_buf},
            timeout=timeout_ms / 1000.0,
        )
        suggested = resp.get("longpolling_timeout_ms")
        if isinstance(suggested, int) and suggested > 0:
            timeout_ms = suggested
        ret = resp.get("ret", 0)
        errcode = resp.get("errcode", 0)
        if ret not in (0, None) or errcode not in (0, None):
            if SESSION_EXPIRED_ERRCODE in (ret, errcode) or _is_stale_session_ret(
                ret, errcode, resp.get("errmsg")
            ):
                raise _SessionExpiredError
            raise RuntimeError(
                "iLink getupdates error: "
                f"ret={ret} errcode={errcode} errmsg={str(resp.get('errmsg') or '')[:200]}"
            )
        new_buf = str(resp.get("get_updates_buf") or "")
        # Deliver every message BEFORE advancing the continuation token — a
        # failure here keeps the buf put, so the same messages are re-fetched
        # next poll instead of being silently dropped (e.g. during a rollout).
        msgs: list[dict[str, Any]] = resp.get("msgs") or []
        for message in msgs:
            await self._handle_message(message)
        if new_buf and new_buf != sync_buf:
            sync_buf = new_buf
            _save_sync_buf(sync_buf)
        return sync_buf, timeout_ms

    async def _handle_message(self, message: dict[str, Any]) -> None:
        """Normalize one iLink message into the core; capture context_token."""
        sender_id = str(message.get("from_user_id") or "").strip()
        if not sender_id or sender_id == self._account_id:
            return
        if message.get("room_id") or message.get("chat_room_id"):
            return  # iLink bots are DM-only; group events are not surfaced
        if message.get("message_type") == MSG_TYPE_BOT:
            return
        text = _extract_text(message.get("item_list") or [])
        if not text:
            return
        message_id = str(message.get("message_id") or "").strip()
        keys = [k for k in (message_id, _content_key(sender_id, text)) if k]
        if any(self._is_duplicate(k) for k in keys):
            return
        for key in keys:
            self._mark_seen(key)
        context_token = str(message.get("context_token") or "").strip()
        if context_token:
            self._tokens.set(sender_id, context_token)
        self._mark_inbound(sender_id)  # extends the iLink active-push window
        await self.core.handle_inbound(
            InboundMessage(
                channel=self.channel,
                chat_id=sender_id,
                text=text,
                message_id=message_id or None,
            )
        )

    def _is_duplicate(self, key: str) -> bool:
        stamp = self._seen.get(key)
        return stamp is not None and time.monotonic() - stamp < DEDUP_TTL_SECONDS

    def _mark_seen(self, key: str) -> None:
        self._seen[key] = time.monotonic()
        if len(self._seen) > 512:  # bound memory; prune expired entries
            cutoff = time.monotonic() - DEDUP_TTL_SECONDS
            self._seen = {k: t for k, t in self._seen.items() if t >= cutoff}

    async def send(
        self,
        chat_id: str,
        text: str,
        *,
        buttons: list[tuple[str, str]] | None = None,
        markdown: bool = False,
    ) -> None:
        """Send plain text, segmenting past 2000 chars; raise on failure.

        ``buttons`` / ``markdown`` are accepted for the shared adapter contract
        but not rendered on WeChat (plain text only)."""

        del buttons, markdown  # platform contract: accepted, not rendered
        if not self._configured:
            raise RuntimeError("weixin not configured — cannot send")
        context_token = self._tokens.get(chat_id)
        chunks = _split_text(text)
        for idx, chunk in enumerate(chunks):
            key = (chat_id, idx)
            now = time.monotonic()
            pending = self._pending_client_ids.pop(key, None)
            if pending is not None and now - pending[1] < _PENDING_CLIENT_ID_TTL_S:
                client_id = pending[0]  # retry of a timed-out send: same id
            else:
                client_id = uuid.uuid4().hex
            try:
                await self._send_chunk(chat_id, chunk, context_token, client_id)
            except Exception:
                self._pending_client_ids[key] = (client_id, now)
                raise
            if idx < len(chunks) - 1 and self._chunk_delay_seconds > 0:
                await asyncio.sleep(self._chunk_delay_seconds)

    async def send_to_owner(self, text: str, *, markdown: bool = False) -> None:
        """Send an outbound notification to the account's own user chat.

        The owner is the user who QR-logged the bot in (account file's
        ``user_id``); the peer context_token may not exist yet for a first
        outbound message — ``send`` attempts without one and iLink either
        accepts it or reports a stale session, which the retry path handles.
        """

        del markdown  # platform contract: accepted, not rendered
        if not self._configured or not self._user_id:
            raise RuntimeError("weixin not configured — cannot notify")
        await self.send(self._user_id, text)

    async def _send_chunk(
        self, chat_id: str, chunk: str, context_token: str | None, client_id: str
    ) -> None:
        """Send one chunk; on a stale session, retry once without the token.

        ``client_id`` stays fixed for this logical send — including the
        stale-session retry — so iLink's dedup collapses duplicates."""
        token = context_token
        retried_without_token = False
        while True:
            resp = await self._post(
                EP_SEND_MESSAGE,
                _outbound_message(chat_id, chunk, token, client_id),
                timeout=API_TIMEOUT_SECONDS,
            )
            ret = resp.get("ret", 0)
            errcode = resp.get("errcode", 0)
            if ret in (0, None) and errcode in (0, None):
                # A successful send either confirms the session or follows a
                # user message that refreshed it — reset the watchdog and
                # remember the recovery moment for the core's "recovered" hint.
                if self.push_failures > 0:
                    self.push_recovered_at = time.time()
                self.push_failures = 0
                return
            session_expired = SESSION_EXPIRED_ERRCODE in (ret, errcode) or (
                _is_stale_session_ret(ret, errcode, resp.get("errmsg"))
            )
            if session_expired and token and not retried_without_token:
                retried_without_token = True
                token = None
                self._tokens.drop(chat_id)
                logger.warning(
                    "weixin session expired for {}; retrying without context_token",
                    _safe_id(chat_id),
                )
                continue
            errmsg = str(resp.get("errmsg") or resp.get("msg") or "unknown error")
            self.push_failures += 1
            self.push_failed_at = time.time()
            raise RuntimeError(
                f"iLink sendmessage error: ret={ret} errcode={errcode} errmsg={errmsg[:200]}"
            )

    async def _post(
        self, endpoint: str, payload: dict[str, Any], *, timeout: float
    ) -> dict[str, Any]:
        """Authenticated iLink POST; sanitized errors (never the token/URL).

        A getUpdates timeout is the long-poll's normal "nothing new" signal and
        returns an empty batch; other timeouts raise.
        """
        try:
            resp = await self._http.post(
                f"{self._base_url}/{endpoint}",
                json={**payload, "base_info": _base_info()},
                headers=_headers(self._token),
                timeout=timeout,
            )
        except httpx.TimeoutException:
            if endpoint == EP_GET_UPDATES:
                return {
                    "ret": 0,
                    "msgs": [],
                    "get_updates_buf": str(payload.get("get_updates_buf") or ""),
                }
            raise RuntimeError("weixin send timed out") from None
        except httpx.HTTPError as exc:
            raise RuntimeError(f"weixin {endpoint} request failed: {type(exc).__name__}") from None
        if resp.status_code != 200:
            raise RuntimeError(f"iLink POST {endpoint} HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()


async def _qr_get(
    client: httpx.AsyncClient, base_url: str, endpoint: str, query: str
) -> dict[str, Any] | None:
    """GET an iLink QR endpoint; None on any transient error (caller retries)."""
    try:
        resp = await client.get(
            f"{base_url.rstrip('/')}/{endpoint}?{query}", headers=_get_headers()
        )
    except httpx.HTTPError as exc:
        logger.warning("weixin: {} failed: {}", endpoint, type(exc).__name__)
        return None
    if resp.status_code != 200:
        logger.warning("weixin: {} failed: HTTP {}", endpoint, resp.status_code)
        return None
    return resp.json()


def _show_qr(qrcode_value: str, qrcode_url: str) -> None:
    """Print the scannable URL, plus a best-effort terminal QR render."""
    logger.info("Scan the QR code below with WeChat: {}", qrcode_url or qrcode_value)
    try:
        import qrcode  # optional dependency

        qr = qrcode.QRCode()
        qr.add_data(qrcode_url or qrcode_value)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:
        logger.info("(terminal QR rendering unavailable — open the link above to scan)")


async def _refresh_qr(
    client: httpx.AsyncClient, base_url: str, refresh_count: int
) -> tuple[str, str] | None:
    """Fetch a fresh QR after the previous one expired; None on failure."""
    logger.info("QR code expired, refreshing… ({}/3)", refresh_count)
    qr = await _qr_get(client, base_url, EP_GET_BOT_QR, "bot_type=3")
    qrcode_value = _get_str(qr, "qrcode")
    qrcode_url = _get_str(qr, "qrcode_img_content")
    if not qrcode_value:
        return None
    _show_qr(qrcode_value, qrcode_url)
    return qrcode_value, qrcode_url


def _confirmed_credentials(status: dict[str, Any]) -> dict[str, Any] | None:
    """Validate a "confirmed" status payload; persist and return credentials."""
    account_id = _get_str(status, "ilink_bot_id")
    bot_token = _get_str(status, "bot_token")
    bot_base_url = _get_str(status, "baseurl") or ILINK_BASE_URL
    user_id = _get_str(status, "ilink_user_id")
    if not account_id or not bot_token:
        logger.error("weixin: QR confirmed but credential payload incomplete")
        return None
    save_account(
        account_id=account_id,
        bot_token=bot_token,
        user_id=user_id,
        base_url=bot_base_url,
    )
    logger.info("WeChat connected, account_id={}", account_id)
    return {
        "account_id": account_id,
        "bot_token": bot_token,
        "base_url": bot_base_url,
        "user_id": user_id,
    }


async def qr_login(
    *,
    timeout_seconds: int = 480,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    """Run the interactive iLink QR login; persist credentials on success."""
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=httpx.Timeout(QR_POLL_TIMEOUT_SECONDS))
    try:
        base_url = ILINK_BASE_URL
        qr = await _qr_get(client, base_url, EP_GET_BOT_QR, "bot_type=3")
        qrcode_value = _get_str(qr, "qrcode")
        qrcode_url = _get_str(qr, "qrcode_img_content")
        if not qrcode_value:
            logger.error("weixin: QR response missing qrcode")
            return None
        _show_qr(qrcode_value, qrcode_url)
        deadline = time.monotonic() + timeout_seconds
        refresh_count = 0
        last_state = ""
        while time.monotonic() < deadline:
            status = await _qr_get(client, base_url, EP_GET_QR_STATUS, f"qrcode={qrcode_value}")
            if status is None:
                await asyncio.sleep(1)
                continue
            state = str(status.get("status") or "wait")
            if state != last_state:
                if state == "wait":
                    logger.info("Waiting for scan…")
                elif state == "scaned":
                    logger.info("Scanned — confirm it in WeChat…")
                elif state == "scaned_but_redirect":
                    redirect_host = str(status.get("redirect_host") or "")
                    if redirect_host:
                        base_url = f"https://{redirect_host}"
                elif state == "expired":
                    refresh_count += 1
                    if refresh_count > 3:
                        logger.warning("QR code expired repeatedly — re-run login.")
                        return None
                    refreshed = await _refresh_qr(client, base_url, refresh_count)
                    if refreshed is None:
                        return None
                    qrcode_value, qrcode_url = refreshed
                last_state = state
            if state == "confirmed":
                creds = _confirmed_credentials(status)
                if creds is None:
                    return None
                return creds
            await asyncio.sleep(1)
        logger.warning("WeChat login timed out.")
        return None
    finally:
        if owns_client:
            await client.aclose()


def main(argv: list[str] | None = None) -> int:
    """CLI entry: ``python -m services.im_bridge.adapters.weixin --login``."""
    parser = argparse.ArgumentParser(
        prog="python -m services.im_bridge.adapters.weixin",
        description="Weixin iLink adapter helper (login / debug)",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Interactive QR login; credentials are written to $AVA_HOME/state/im_bridge/weixin_account.json",
    )
    args = parser.parse_args(argv)
    if args.login:
        creds = asyncio.run(qr_login())
        return 0 if creds else 1
    parser.print_help()
    return 1


# The daemon's _load_adapters looks up this name on every adapter module.
ADAPTER_CLASS = WeixinAdapter

if __name__ == "__main__":
    raise SystemExit(main())
