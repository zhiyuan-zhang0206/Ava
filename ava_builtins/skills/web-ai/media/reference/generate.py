"""web-ai media — generate an image or video and land the file in Downloads.

The agent runs this as a subprocess; it drives the user's logged-in browser to
ChatGPT / Gemini, sends an image- or video-generation prompt, waits for the
asset to render, downloads it, and prints JSON (with the saved path) to stdout.

    # Image — usually ~10-60s, fetched in one call:
    .venv/bin/python skills/web-ai/media/reference/generate.py image \
        --site gemini --prompt "a photorealistic red panda on a skateboard, golden hour"

    # Video (Gemini) — can be slower; submit returns a conversation URL, then poll:
    .venv/bin/python skills/web-ai/media/reference/generate.py video --site gemini --prompt "..."
    .venv/bin/python skills/web-ai/media/reference/generate.py check --site gemini --kind video --url "<url>"

The asset lands at `~/Downloads/ava_<cluster>_web-ai/media/<stamp>-<site>-<slug>/`.
The result JSON always includes the asset's `src` URL, so even if the download
step fails the agent still has the link.

Download paths: an `https://` asset is fetched with curl; a `blob:` image is
re-encoded from the rendered <img> via a canvas (blob URLs are same-origin, so
this works); a `data:` URL is decoded directly. Video blobs (MediaSource) cannot
be re-encoded that way — for those the result reports the URL and the agent
delivers via the site's own download control.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

_WEBCHAT_PATH = Path(__file__).resolve().parents[2] / "reference" / "webchat.py"
_spec = importlib.util.spec_from_file_location("webchat", _WEBCHAT_PATH)
if _spec is None or _spec.loader is None:
    raise RuntimeError(f"cannot load shared driver at {_WEBCHAT_PATH}")
webchat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(webchat)

# Phrasing the prompt as an explicit generation request makes the model produce
# the asset rather than describe it. --raw sends the prompt verbatim.
_IMAGE_PREFIX = "Generate an image: "
_VIDEO_PREFIX = "Generate a video: "

IMAGE_SITES = ("chatgpt", "gemini")
VIDEO_SITES = ("gemini",)  # ChatGPT video (Sora) lives on a separate app, out of scope for v1


def _slug(text: str, *, max_len: int = 40) -> str:
    words = re.findall(r"[A-Za-z0-9]+", text.lower())
    return "-".join(words)[:max_len].strip("-") or "media"


# --------------------------------------------------------------------------- #
# Locate the generated asset on the page
# --------------------------------------------------------------------------- #


def _image_srcs(*, min_px: int) -> list[str]:
    """Srcs of every img already at least `min_px` on both sides — the
    pre-submit baseline `_wait_for_image` excludes, so a logo / avatar that
    predates the prompt (ChatGPT's home page carries a 512px GPT logo) can
    never read as the generated asset."""
    js = (
        "() => {"
        f"  const MIN = {min_px};"
        "  return [...document.querySelectorAll('img')]"
        "    .filter((i) => (i.naturalWidth || i.width) >= MIN && (i.naturalHeight || i.height) >= MIN)"
        "    .map((i) => i.currentSrc || i.src || '').filter(Boolean);"
        "}"
    )
    return webchat.evaluate(js)


def _find_image_src(*, min_px: int, exclude: list[str]) -> dict[str, Any]:
    """The last <img> on the page at least `min_px` on both sides whose src is
    not in `exclude` (skips avatars, icons, inline svg, and anything that was
    already on the page before the prompt). Returns {src, w, h} or {src: None}."""
    js = (
        "() => {"
        f"  const MIN = {min_px};"
        f"  const EXCLUDE = new Set({json.dumps(exclude)});"
        "  const imgs = [...document.querySelectorAll('img')].filter((i) => {"
        "    const w = i.naturalWidth || i.width, h = i.naturalHeight || i.height;"
        "    const s = i.currentSrc || i.src || '';"
        "    return w >= MIN && h >= MIN && s && !s.startsWith('data:image/svg') && !EXCLUDE.has(s);"
        "  });"
        "  if (!imgs.length) return { src: null };"
        "  const el = imgs[imgs.length - 1];"
        "  return { src: el.currentSrc || el.src, w: el.naturalWidth || el.width, h: el.naturalHeight || el.height };"
        "}"
    )
    return webchat.evaluate(js)


def _find_video_src() -> dict[str, Any]:
    """The last <video> on the page; reports its src and a nearby download link
    href if one exists. Returns {src, download, poster} (any may be null)."""
    js = (
        "() => {"
        "  const vids = [...document.querySelectorAll('video')];"
        "  if (!vids.length) return { src: null };"
        "  const el = vids[vids.length - 1];"
        "  const srcEl = el.querySelector('source');"
        "  const src = el.currentSrc || el.src || (srcEl ? srcEl.src : null);"
        "  const dl = [...document.querySelectorAll('a[download], a[href*=\".mp4\"]')]"
        "    .map((a) => a.href).find(Boolean) || null;"
        "  return { src, download: dl, poster: el.poster || null };"
        "}"
    )
    return webchat.evaluate(js)


def _wait_for_image(*, timeout: float, min_px: int, exclude: list[str]) -> dict[str, Any]:

    deadline = time.monotonic() + timeout
    while True:
        found = _find_image_src(min_px=min_px, exclude=exclude)
        if found.get("src"):
            return found
        if time.monotonic() >= deadline:
            return {"src": None}
        time.sleep(1.5)


def _wait_for_video(*, timeout: float) -> dict[str, Any]:

    deadline = time.monotonic() + timeout
    while True:
        found = _find_video_src()
        if found.get("src") or found.get("download"):
            return found
        if time.monotonic() >= deadline:
            return {"src": None}
        time.sleep(3.0)


def _wait_for_conversation_url(site: str, *, timeout: float) -> str:
    """Poll until the page URL becomes a per-conversation URL (it changes once the
    first message is sent), so a returned poll handle points at the real chat,
    not the blank new-chat page."""

    base = webchat.site(site)["new_chat_url"].rstrip("/")
    deadline = time.monotonic() + timeout
    while True:
        url = webchat.current_url()
        if url.rstrip("/") != base and re.search(r"/[A-Za-z0-9_-]{6,}", url.removeprefix(base)):
            return url
        if time.monotonic() >= deadline:
            return webchat.current_url()
        time.sleep(1.0)


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #


def _canvas_data_url(src: str) -> str | None:
    """Re-encode the rendered <img> with this src to a PNG data URL via a canvas.
    Works for blob: and same-origin https sources; None if the <img> is gone or
    the canvas is tainted (cross-origin src without CORS)."""
    js = (
        "() => {"
        f"  const SRC = {json.dumps(src)};"
        "  const img = [...document.querySelectorAll('img')].find((i) => (i.currentSrc || i.src) === SRC);"
        "  if (!img) return { dataUrl: null, err: 'img-gone' };"
        "  const c = document.createElement('canvas');"
        "  c.width = img.naturalWidth; c.height = img.naturalHeight;"
        "  c.getContext('2d').drawImage(img, 0, 0);"
        "  try { return { dataUrl: c.toDataURL('image/png') }; }"
        "  catch (e) { return { dataUrl: null, err: String(e) }; }"
        "}"
    )
    return webchat.evaluate(js).get("dataUrl")


def _ext_from_url(src: str, *, default: str) -> str:
    m = re.search(r"\.(png|jpg|jpeg|webp|gif|mp4|webm|mov)(?:[?#]|$)", src, re.I)
    return m.group(1).lower() if m else default


def _curl_args(url: str, dest: Path, *, max_filesize: str, max_time: str) -> list[str]:
    """Hardened curl argv: https only (including across redirects), bounded
    redirects / size / time. The asset URL comes from the page DOM, so pinning
    the protocol blocks a redirect to http://localhost or a metadata IP."""
    return [
        "curl",
        "-fsSL",
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "--max-redirs",
        "5",
        "--max-filesize",
        max_filesize,
        "--max-time",
        max_time,
        "-o",
        str(dest),
        url,
    ]


def _save_image(src: str, dest_dir: Path) -> Path:
    """Save the image at `src` under `dest_dir`; returns the file path. Raises on
    a non-fetchable src, a curl failure, or a tainted-canvas blob."""
    if src.startswith("data:image"):
        b64 = src.split(",", 1)[1]
        dest = dest_dir / "image.png"
        dest.write_bytes(base64.b64decode(b64))
        return dest
    if src.startswith("https://"):
        # The img is already rendered inside the authed page, so canvas
        # re-encode first — an https asset behind session auth 403s for a
        # cookie-less curl. A cross-origin img taints the canvas (returns
        # None); then curl fetches it (public CDN case).
        data_url = _canvas_data_url(src)
        if data_url:
            dest = dest_dir / "image.png"
            dest.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
            return dest
        dest = dest_dir / f"image.{_ext_from_url(src, default='png')}"
        subprocess.run(_curl_args(src, dest, max_filesize="100M", max_time="120"), check=True)
        _assert_media_bytes(dest)
        return dest
    if src.startswith("blob:"):
        data_url = _canvas_data_url(src)
        if not data_url:
            raise RuntimeError(f"could not re-encode blob image (tainted canvas?): {src!r}")
        dest = dest_dir / "image.png"
        dest.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
        return dest
    raise ValueError(f"refusing to download non-https/blob/data image src: {src[:80]!r}")


def _assert_media_bytes(path: Path) -> None:
    """Raise if the downloaded file is an HTML page, not media — an auth
    interstitial downloads with HTTP 200, so curl's -f cannot catch it."""
    head = path.read_bytes()[:256].lstrip().lower()
    if head.startswith((b"<!doctype", b"<html", b"<head", b"<script")):
        path.unlink()
        raise RuntimeError("fetched an HTML page (auth interstitial), not the media bytes")


def _browser_download_video(dest_dir: Path, *, timeout: float = 60.0) -> Path | None:
    """Click the player's own download control and collect the landed file —
    the browser carries the session auth that a bare curl on the asset URL does
    not. Returns None when no control was found or no file landed in time."""
    return webchat.download_by_click(
        ["Download video", "Download"],
        dest=dest_dir / "video.mp4",
        suffixes=(".mp4",),
        timeout=timeout,
    )


def _save_video_url(url: str, dest_dir: Path) -> Path:
    """Fetch a video over plain https, validated to actually be media bytes."""
    dest = dest_dir / f"video.{_ext_from_url(url, default='mp4')}"
    subprocess.run(_curl_args(url, dest, max_filesize="500M", max_time="300"), check=True)
    _assert_media_bytes(dest)
    return dest


def _attempt_image(src: str, dest_dir: Path) -> tuple[str | None, str | None]:
    """Download the image, never raising: returns (path, note). On any failure
    path is None and note explains — the caller keeps the src link in its result."""
    try:
        return str(_save_image(src, dest_dir)), None
    except (subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
        return (
            None,
            f"download failed ({type(exc).__name__}: {exc}); the src link is in this result",
        )


def _attempt_video(
    src: str | None, download: str | None, dest_dir: Path
) -> tuple[str | None, str | None]:
    """Download the video, never raising: returns (path, note).

    Order: the player's own download button first (the browser carries the
    session auth — Gemini's asset URL serves an auth interstitial to a bare
    curl), then a direct https fetch validated to be media bytes."""
    path = _browser_download_video(dest_dir)
    if path is not None:
        return str(path), None
    url = next((u for u in (download, src) if u and u.startswith("https://")), None)
    if url is None:
        return (
            None,
            "video src is a blob/MediaSource and no download control found — use the site's button",
        )
    try:
        return str(_save_video_url(url, dest_dir)), None
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        return (
            None,
            f"download failed ({type(exc).__name__}: {exc}); the src link is in this result",
        )


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def _close_on_done(
    page_id: int | None, result: dict[str, Any], *, keep_tab: bool
) -> dict[str, Any]:
    """One-shot cleanup for a terminal (done) result: close the owned tab unless
    --keep-tab, then stamp `tab_kept` onto the result. Non-terminal results keep
    their tab and set tab_kept themselves (the work continues, polled via url)."""
    if not keep_tab:
        webchat.close_tab(page_id)
    result["tab_kept"] = keep_tab
    return result


def cmd_image(args: argparse.Namespace) -> dict[str, Any]:
    page_id = webchat.open_chat(args.site)
    # Baseline BEFORE submitting: only an img that appears after the prompt can
    # be the generated asset.
    baseline = _image_srcs(min_px=args.min_px)
    prompt = args.prompt if args.raw else _IMAGE_PREFIX + args.prompt
    webchat.inject_prompt(args.site, prompt)
    via = webchat.submit(args.site)
    # Conversation URL first — it confirms the submit actually landed and is
    # the poll handle a pending result hands back.
    url = _wait_for_conversation_url(args.site, timeout=25.0)
    found = _wait_for_image(timeout=args.timeout, min_px=args.min_px, exclude=baseline)
    if not found.get("src"):
        # Not done — the image is still rendering and gets collected later via
        # `check --url`, so the tab stays open regardless of --keep-tab.
        return {
            "state": "pending",
            "kind": "image",
            "url": url,
            "submitted_via": via,
            "tab_kept": True,
            "note": "no image detected within timeout; re-run: check --kind image --url <url>",
        }
    dest_dir = (
        webchat.downloads_root("media") / f"{webchat.now_stamp()}-{args.site}-{_slug(args.prompt)}"
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    path, note = _attempt_image(found["src"], dest_dir)
    return _close_on_done(
        page_id,
        {
            "state": "done",
            "kind": "image",
            "url": url,
            "src": found["src"],
            "path": path,
            "note": note,
            "dir": str(dest_dir),
            "w": found.get("w"),
            "h": found.get("h"),
        },
        keep_tab=args.keep_tab,
    )


def cmd_video(args: argparse.Namespace) -> dict[str, Any]:
    page_id = webchat.open_chat(args.site)
    prompt = args.prompt if args.raw else _VIDEO_PREFIX + args.prompt
    webchat.inject_prompt(args.site, prompt)
    via = webchat.submit(args.site)
    # Read the per-conversation URL (not the still-blank /app) so the poll handle
    # the agent gets back actually points at this generation.
    url = _wait_for_conversation_url(args.site, timeout=25.0)
    # Video generation is slow; don't block a turn on it by default. With --wait
    # the agent asked to wait in-turn; otherwise return the URL to poll via check.
    if not args.wait:
        # Still generating, collected later via `check --url` — keep the tab.
        return {
            "state": "submitted",
            "kind": "video",
            "url": url,
            "submitted_via": via,
            "tab_kept": True,
            "note": "poll with: check --kind video --url <url>",
        }
    res = _collect_video(args.site, url, timeout=args.timeout, prompt=args.prompt)
    if res["state"] == "done":
        return _close_on_done(page_id, res, keep_tab=args.keep_tab)
    res["tab_kept"] = True  # still pending — keep the tab to poll
    return res


def _collect_video(site: str, url: str, *, timeout: float, prompt: str) -> dict[str, Any]:
    found = _wait_for_video(timeout=timeout)
    if not found.get("src") and not found.get("download"):
        return {
            "state": "pending",
            "kind": "video",
            "url": url,
            "note": "no video yet; re-run: check --kind video --url <url>",
        }
    dest_dir = webchat.downloads_root("media") / f"{webchat.now_stamp()}-{site}-{_slug(prompt)}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    path, note = _attempt_video(found.get("src"), found.get("download"), dest_dir)
    return {
        "state": "done",
        "kind": "video",
        "url": url,
        "src": found.get("src"),
        "download": found.get("download"),
        "path": path,
        "dir": str(dest_dir),
        "note": note,
    }


def cmd_check(args: argparse.Namespace) -> dict[str, Any]:
    webchat.assert_same_site(args.url, args.site)
    page_id = webchat.navigate(args.url)

    time.sleep(2.0)
    if args.kind == "image":
        # No pre-submit baseline exists on a revisit; on the conversation page
        # the generated asset is the dominant img, so exclude nothing.
        found = _find_image_src(min_px=args.min_px, exclude=[])
        if not found.get("src"):
            return {"state": "running", "kind": "image", "url": args.url, "tab_kept": True}
        dest_dir = webchat.downloads_root("media") / f"{webchat.now_stamp()}-{args.site}-image"
        dest_dir.mkdir(parents=True, exist_ok=True)
        path, note = _attempt_image(found["src"], dest_dir)
        return _close_on_done(
            page_id,
            {
                "state": "done",
                "kind": "image",
                "url": args.url,
                "src": found["src"],
                "path": path,
                "note": note,
                "dir": str(dest_dir),
            },
            keep_tab=args.keep_tab,
        )
    found = _find_video_src()
    if not found.get("src") and not found.get("download"):
        return {"state": "running", "kind": "video", "url": args.url, "tab_kept": True}
    res = _collect_video(args.site, args.url, timeout=1.0, prompt="video")
    if res["state"] == "done":
        return _close_on_done(page_id, res, keep_tab=args.keep_tab)
    res["tab_kept"] = True
    return res


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate an image or video and save it to Downloads."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_img = sub.add_parser("image", help="generate an image and download it")
    p_img.add_argument("--site", required=True, choices=sorted(IMAGE_SITES))
    p_img.add_argument("--prompt", required=True)
    p_img.add_argument(
        "--raw",
        action="store_true",
        help="send the prompt verbatim (no 'Generate an image:' prefix)",
    )
    p_img.add_argument(
        "--timeout", type=float, default=120.0, help="seconds to wait for the image (default 120)"
    )
    p_img.add_argument(
        "--min-px",
        type=int,
        default=256,
        help="min side length to treat an <img> as the result (default 256)",
    )
    p_img.add_argument(
        "--keep-tab",
        action="store_true",
        help="leave the browser tab open when done (default: close it once the image is saved)",
    )
    p_img.set_defaults(fn=cmd_image)

    p_vid = sub.add_parser("video", help="generate a video (submit, then poll with check)")
    p_vid.add_argument("--site", required=True, choices=sorted(VIDEO_SITES))
    p_vid.add_argument("--prompt", required=True)
    p_vid.add_argument("--raw", action="store_true")
    p_vid.add_argument(
        "--wait",
        action="store_true",
        help="wait in-turn for the video instead of returning to poll",
    )
    p_vid.add_argument(
        "--timeout", type=float, default=300.0, help="seconds to wait when --wait (default 300)"
    )
    p_vid.add_argument(
        "--keep-tab",
        action="store_true",
        help="leave the browser tab open when a --wait run finishes (default: close it)",
    )
    p_vid.set_defaults(fn=cmd_video)

    p_chk = sub.add_parser("check", help="re-check a submitted generation and download when ready")
    p_chk.add_argument("--site", required=True, choices=sorted(IMAGE_SITES))
    p_chk.add_argument("--kind", required=True, choices=("image", "video"))
    p_chk.add_argument("--url", required=True)
    p_chk.add_argument("--min-px", type=int, default=256)
    p_chk.add_argument(
        "--keep-tab",
        action="store_true",
        help="leave the browser tab open when the result is ready (default: close it)",
    )
    p_chk.set_defaults(fn=cmd_check)

    args = parser.parse_args()
    print(json.dumps(args.fn(args), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
