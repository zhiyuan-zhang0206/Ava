#!/usr/bin/env python3
"""Send one local file to the user's Telegram chat via the Bot API.

The only direct Telegram Bot API call an agent may make (user ruling
2026-08-13): a one-way ``sendDocument`` to the configured owner chat. Reading
updates, sending text, or any other Bot API method stays forbidden — text
delivery goes through IM Bridge.

Run from the Ava source root with the venv Python:

    .venv/bin/python .agents/skills/telegram-send-file/scripts/send_file.py <path> [--caption TEXT]

Credentials come from the shared cluster config (``settings.telegram.*``,
env ``AVA_TELEGRAM_BOT_TOKEN`` / ``AVA_TELEGRAM_OWNER_ID``) — the same source
IM Bridge uses. Nothing is hardcoded, and the token never appears in output:
httpx exceptions embed the request URL, which carries the token, so every
error path is sanitized before it reaches the caller (the same discipline as
the im_bridge telegram adapter).

Exit code 0 with a one-line summary on success; 1 with ``error: <reason>``
on stderr on failure.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

# Telegram's hard cap for documents sent through the Bot API.
MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
# Telegram's caption cap for sendDocument.
MAX_CAPTION_CHARS = 1024
# Uploads up to 50 MB over a private link need more headroom than a text
# message; a slow link should fail loudly rather than hang the caller.
_SEND_TIMEOUT_S = 120.0


def _source_root() -> Path:
    """The checkout / install root that holds the ``shared`` package.

    The script is invoked from two places: the dev checkout (``.agents/
    skills/...`` — walk up to the repo root) and the prod install
    (``$AVA_HOME/skills/...`` — a converge copy; ``shared`` lives in
    ``$AVA_HOME/source``). ``shared.config`` must be importable from either,
    so the root is resolved before the import happens.
    """

    here = Path(__file__).resolve().parent
    for cand in (here, *here.parents):
        if (cand / "shared" / "__init__.py").is_file():
            return cand
    home = Path(os.environ.get("AVA_HOME", "~/.ava")).expanduser()
    cand = home / "source"
    if (cand / "shared" / "__init__.py").is_file():
        return cand
    raise RuntimeError(
        f"cannot locate the Ava source root: no `shared` package above {here} and none at {cand}"
    )


sys.path.insert(0, str(_source_root()))

from shared.config import settings  # noqa: E402 - after the sys.path setup above


def validate_file(path: str | Path) -> Path:
    """Return ``path`` as a resolved regular file, or raise.

    Gates: exists, regular file, non-empty, within Telegram's 50 MB document
    cap. A directory, a missing path, an empty file, or an oversized file
    all fail fast with a message the caller can show the user.
    """

    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"not a file: {p}")
    size = p.stat().st_size
    if size <= 0:
        raise ValueError(f"empty file: {p}")
    if size > MAX_DOCUMENT_BYTES:
        raise ValueError(
            "file exceeds Telegram's sendDocument cap "
            f"({MAX_DOCUMENT_BYTES // (1024 * 1024)} MB): {p} ({size} bytes)"
        )
    return p


def _api_url(token: str) -> str:
    return f"https://api.telegram.org/bot{token}/sendDocument"


def _credentials_from_env(env: Mapping[str, str]) -> tuple[str, int]:
    """Parse the two aliases from an env mapping; malformed owner id -> 0.

    Pure (env passed in) so tests exercise the exact precedence logic
    without touching the process environment.
    """

    token = env.get("AVA_TELEGRAM_BOT_TOKEN", "")
    owner_raw = env.get("AVA_TELEGRAM_OWNER_ID", "0")
    try:
        owner = int(owner_raw)
    except ValueError:
        owner = 0
    return token, owner


def _credentials(env: Mapping[str, str] | None = None) -> tuple[str, int]:
    """Bot token + owner chat id, from the cluster's env aliases.

    Agent processes may NOT construct the ``telegram`` settings domain
    (per-process config, Task #856: the agent profile excludes it, and the
    consumption-matrix guard keeps it that way). The canonical env aliases
    are read directly instead — ``load_ava_env`` populates os.environ from
    $AVA_HOME/.env, the same source im_bridge's settings read. When the
    aliases are empty (test pins, or a bare CLI context), fall back to the
    settings domain when this process may construct it (no profile marker).
    """

    token, owner = _credentials_from_env(env if env is not None else os.environ)
    if not token and settings.has_domain("telegram"):
        token = settings.telegram.telegram_bot_token
        owner = settings.telegram.telegram_owner_id
    return token, owner


def send_document(
    *,
    token: str,
    chat_id: int,
    path: Path,
    caption: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """POST one file to Telegram's sendDocument; return the result object.

    The result carries ``message_id`` (and the delivered message). Errors are
    sanitized: httpx exceptions can embed the request URL, which contains the
    bot token, so only the exception class name is surfaced; the response
    body (which never contains the token) is included for status failures.
    """

    if caption is not None and len(caption) > MAX_CAPTION_CHARS:
        raise ValueError(f"caption exceeds {MAX_CAPTION_CHARS} chars")
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    owns = client is None
    http = client or httpx.Client(timeout=_SEND_TIMEOUT_S)
    data: dict[str, object] = {"chat_id": chat_id}
    if caption is not None:
        data["caption"] = caption
    try:
        with path.open("rb") as fh:
            resp = http.post(
                _api_url(token),
                data=data,
                files={"document": (path.name, fh, content_type)},
                timeout=_SEND_TIMEOUT_S,
            )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"telegram sendDocument failed: {type(exc).__name__}") from None
    finally:
        if owns:
            http.close()
    if resp.status_code != 200:
        raise RuntimeError(
            f"telegram sendDocument failed: HTTP {resp.status_code} - {resp.text[:200]}"
        )
    payload = resp.json()
    if not payload.get("ok"):
        raise RuntimeError(
            f"telegram sendDocument failed: API {payload.get('description', 'unknown error')}"
        )
    return payload["result"]


def _deliver(path_arg: str, caption: str | None) -> str:
    """Validate and send; return the one-line success report.

    Every failure raises (FileNotFoundError / ValueError / RuntimeError) so
    ``main`` can render one uniform ``error: <reason>`` line; keeping the
    raises outside any try block also keeps TRY301 happy.
    """

    path = validate_file(path_arg)
    if caption is not None and len(caption) > MAX_CAPTION_CHARS:
        raise ValueError(f"caption exceeds {MAX_CAPTION_CHARS} chars")
    token, chat_id = _credentials()
    if not token:
        raise RuntimeError("Telegram bot token not configured (AVA_TELEGRAM_BOT_TOKEN)")
    if not chat_id:
        raise RuntimeError("Telegram owner chat not configured (AVA_TELEGRAM_OWNER_ID)")
    result = send_document(token=token, chat_id=chat_id, path=path, caption=caption)
    return (
        f"sent {path.name} ({path.stat().st_size} bytes) to chat {chat_id}: "
        f"message_id={result['message_id']}"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: validate, send, report. Returns the exit code."""

    parser = argparse.ArgumentParser(
        prog="telegram-send-file",
        description="Send one local file to the user's Telegram chat (Bot API sendDocument).",
    )
    parser.add_argument("path", help="local file path to deliver")
    parser.add_argument(
        "--caption",
        default=None,
        help="optional plain-text caption (at most 1024 chars)",
    )
    args = parser.parse_args(argv)
    try:
        print(_deliver(args.path, args.caption))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
