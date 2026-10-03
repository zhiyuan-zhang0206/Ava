"""The interactive iLink QR login of the WeChat adapter (``python -m ...weixin --login``).

Split out of `weixin.py` (the adapter itself): the login runs once, from the CLI, and persists
the account the adapter later loads.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from base.log import logger
from services.im_bridge.adapters.weixin import (
    EP_GET_BOT_QR,
    EP_GET_QR_STATUS,
    ILINK_BASE_URL,
    QR_POLL_TIMEOUT_SECONDS,
    _get_headers,
    _get_str,
    save_account,
)


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


@dataclass
class _QrSession:
    """The mutable edges of one QR login: where to poll and which code is live."""

    base_url: str
    qrcode_value: str
    refresh_count: int = 0


async def _on_qr_state(
    client: httpx.AsyncClient, session: _QrSession, state: str, status: dict[str, Any]
) -> bool:
    """React to a QR status change; False when the login cannot continue."""
    if state == "wait":
        logger.info("Waiting for scan…")
    elif state == "scaned":
        logger.info("Scanned — confirm it in WeChat…")
    elif state == "scaned_but_redirect":
        redirect_host = str(status.get("redirect_host") or "")
        if redirect_host:
            session.base_url = f"https://{redirect_host}"
    elif state == "expired":
        session.refresh_count += 1
        if session.refresh_count > 3:
            logger.warning("QR code expired repeatedly — re-run login.")
            return False
        refreshed = await _refresh_qr(client, session.base_url, session.refresh_count)
        if refreshed is None:
            return False
        session.qrcode_value = refreshed[0]
    return True


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
        qr = await _qr_get(client, ILINK_BASE_URL, EP_GET_BOT_QR, "bot_type=3")
        qrcode_value = _get_str(qr, "qrcode")
        qrcode_url = _get_str(qr, "qrcode_img_content")
        if not qrcode_value:
            logger.error("weixin: QR response missing qrcode")
            return None
        _show_qr(qrcode_value, qrcode_url)
        session = _QrSession(base_url=ILINK_BASE_URL, qrcode_value=qrcode_value)
        deadline = time.monotonic() + timeout_seconds
        last_state = ""
        while time.monotonic() < deadline:
            status = await _qr_get(
                client, session.base_url, EP_GET_QR_STATUS, f"qrcode={session.qrcode_value}"
            )
            if status is None:
                await asyncio.sleep(1)
                continue
            state = str(status.get("status") or "wait")
            if state != last_state:
                if not await _on_qr_state(client, session, state, status):
                    return None
                last_state = state
            if state == "confirmed":
                return _confirmed_credentials(status)
            await asyncio.sleep(1)
        logger.warning("WeChat login timed out.")
        return None
    finally:
        if owns_client:
            await client.aclose()
