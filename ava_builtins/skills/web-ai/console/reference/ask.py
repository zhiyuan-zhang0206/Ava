"""web-ai console — ask the same question to several frontier models at once.

The agent runs this as a subprocess, e.g.

    .venv/bin/python skills/web-ai/console/reference/ask.py --prompt "<hard question>"
    .venv/bin/python skills/web-ai/console/reference/ask.py --models chatgpt,gemini --prompt "..."
    echo "<long multi-line question>" | .venv/bin/python skills/web-ai/console/reference/ask.py

and reads the JSON printed to stdout. It opens a fresh chat per model in the
user's logged-in browser, types the prompt into each, and then waits on them all
at once — every model streams its answer concurrently in its own tab, so the
wall-clock is the slowest single answer, not the sum. The combined answers land
at `~/Downloads/ava_<cluster>_web-ai/console/<stamp>-<slug>/` (answers.md +
result.json) and the same structure is printed to stdout so the agent can read
and synthesize without opening a file.

This spends no API credits — it uses the flat-rate web subscriptions the user
already pays for. Each model answers in its own fresh conversation, independent
of the others.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

# Load the shared driver (skills/web-ai/reference/webchat.py) by path, so this
# runs the same whatever the cwd. parents[2] is the web-ai skill root.
_WEBCHAT_PATH = Path(__file__).resolve().parents[2] / "reference" / "webchat.py"
_spec = importlib.util.spec_from_file_location("webchat", _WEBCHAT_PATH)
if _spec is None or _spec.loader is None:
    raise RuntimeError(f"cannot load shared driver at {_WEBCHAT_PATH}")
webchat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(webchat)

_DEFAULT_MODELS = ["chatgpt", "gemini", "claude"]


def _slug(text: str, *, max_len: int = 40) -> str:
    """A short filesystem-safe slug from the prompt's first words."""
    words = re.findall(r"[A-Za-z0-9]+", text.lower())
    slug = "-".join(words)[:max_len].strip("-")
    return slug or "query"


def _to_row(r: dict[str, Any]) -> dict[str, Any]:
    """Map one `webchat.ask_many` row to the console result row the agent reads.

    A setup-failure row (carrying `error`) becomes an `ok: False` row. A completed
    row is `ok` only when it has text AND finished: `ok` requires completion, not
    just text, since a timed-out run can hand back a transient placeholder (e.g.
    ChatGPT's "Thinking") as the partial.
    """
    if "error" in r:
        return {
            "site": r["site"],
            "label": r.get("label", r["site"]),
            "ok": False,
            "error": r["error"],
        }
    return {
        "site": r["site"],
        "label": r["label"],
        "ok": bool(r["answer"]) and r["complete"],
        "complete": r["complete"],
        "chars": len(r["answer"]),
        "url": r["url"],
        "chat_id": r.get("chat_id"),
        "tab_kept": r["tab_kept"],
        "submitted_via": r["submitted_via"],
        "answer": r["answer"],
    }


def _render_markdown(prompt: str, results: list[dict[str, Any]]) -> str:
    lines = ["# Console — multi-model answers", "", "## Question", "", prompt, ""]
    for r in results:
        label = r.get("label", r["site"])
        lines += ["---", "", f"## {label}"]
        if not r["ok"]:
            lines += ["", f"_failed: {r.get('error', 'no answer')}_", ""]
            continue
        if not r.get("complete", True):
            lines += ["", "_(answer did not finish within the timeout — partial)_"]
        lines += ["", r["answer"], ""]
    return "\n".join(lines)


def _save(prompt: str, results: list[dict[str, Any]]) -> Path:
    outdir = webchat.downloads_root("console") / f"{webchat.now_stamp()}-{_slug(prompt)}"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "answers.md").write_text(_render_markdown(prompt, results), encoding="utf-8")
    (outdir / "result.json").write_text(
        json.dumps({"prompt": prompt, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return outdir


def _read_prompt(arg: str | None) -> str:
    if arg is not None:
        return arg
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    sys.exit("no prompt: pass --prompt '...' or pipe the question on stdin")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask several frontier models the same question.")
    parser.add_argument("--prompt", help="the question; if omitted, read from stdin")
    parser.add_argument(
        "--models",
        default=",".join(_DEFAULT_MODELS),
        help=f"comma-separated subset of {', '.join(webchat.SITES)} (default: all)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="seconds to wait for each model's answer to finish (default 180)",
    )
    parser.add_argument(
        "--file",
        help="attach a local file to the question (sent to every model asked)",
    )
    parser.add_argument(
        "--continue-url",
        help="continue an existing conversation by URL instead of opening a fresh chat "
        "(requires exactly one model — the conversation belongs to one site). "
        "Prefer --chat-id with the chat_id from a previous result.",
    )
    parser.add_argument(
        "--chat-id",
        help="continue an existing conversation by chat ID (from a previous result's "
        "chat_id field) instead of opening a fresh chat. Exactly one --models site required.",
    )
    parser.add_argument(
        "--keep-tab",
        action="store_true",
        help="leave each model's browser tab open after answering (default: close "
        "it — the answer is already captured here; resume later with --continue-url <url>)",
    )
    args = parser.parse_args()

    # Resolve the continue target: --chat-id builds the URL via the site profile.
    continue_url = args.continue_url
    if args.chat_id:
        if continue_url:
            sys.exit("pass --chat-id or --continue-url, not both")
        # We need a site to resolve the chat_id -> URL. Use the first model.
        models_early = [m.strip() for m in args.models.split(",") if m.strip()]
        if not models_early:
            sys.exit("--chat-id requires at least one --models site to resolve the URL")
        try:
            continue_url = webchat.chat_url(models_early[0], args.chat_id)
        except Exception as exc:
            sys.exit(f"bad --chat-id {args.chat_id!r}: {exc}")

    prompt = _read_prompt(args.prompt)
    if not prompt.strip():
        sys.exit("empty prompt")
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    unknown = [m for m in models if m not in webchat.SITES]
    if unknown:
        sys.exit(f"unknown model(s): {', '.join(unknown)}; known: {', '.join(webchat.SITES)}")

    if continue_url and len(models) != 1:
        sys.exit(
            "--continue-url/--chat-id requires exactly one --models site (the conversation belongs to one site)"
        )
    specs = [
        {"name": m, "prompt": prompt, "file": args.file, "conversation_url": continue_url}
        for m in models
    ]
    # Login check happens inside open_chat() (called by ask_many for fresh chats):
    # when no composer is found, it detects login walls and attempts auto-login
    # via "Continue with Google".  A login failure becomes an error row in the
    # result — one model's sign-in wall never sinks the others.
    rows = webchat.ask_many(specs, timeout=args.timeout, keep_tab=args.keep_tab)
    results = [_to_row(r) for r in rows]
    outdir = _save(prompt, results)

    if args.keep_tab:
        # stdout is the JSON contract the agent parses; the kept-tab note (and
        # each resume url) lives in the result rows. Mirror a human breadcrumb to
        # stderr so it is visible in a terminal without disturbing that contract.
        kept = [r["url"] for r in results if r.get("tab_kept") and r.get("url")]
        if kept:
            print(f"[console] left {len(kept)} tab(s) open: {', '.join(kept)}", file=sys.stderr)

    print(
        json.dumps(
            {
                "prompt": prompt,
                "dir": str(outdir),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
