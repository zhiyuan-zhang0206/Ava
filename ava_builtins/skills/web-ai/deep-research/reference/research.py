"""web-ai deep-research — run Gemini / ChatGPT / Perplexity Deep Research and fetch the report.

Deep Research is a long job (minutes, not seconds) with an optional plan-approval
step, so it does not fit one turn. This CLI splits it into two deterministic
pieces the agent runs with bash; the agent itself waits between them with
`ava.watcher.at` (a time reminder) and drives the browser in its own turn:

    # 1. kick it off (enable Deep Research, type the prompt, submit, approve plan):
    .venv/bin/python skills/web-ai/deep-research/reference/research.py start \
        --site gemini --prompt "<research question>"
    # -> prints {"site","url","state"} ; remember the url

    # 2. later (the agent schedules ava.watcher.at(+8min) then idles), poll:
    .venv/bin/python skills/web-ai/deep-research/reference/research.py check \
        --site gemini --url "<url from start>"
    # -> {"state":"running"} (reschedule + idle) or
    #    {"state":"done","dir":...,"chars":...} (deliver the report)

The report lands at `~/Downloads/ava_<cluster>_web-ai/deep-research/<stamp>-<site>-<slug>/`
(report.md + meta.json) and the JSON is printed to stdout.

This is the most UI-fragile child: enabling Deep Research mode and judging
"done" depend on selectors / button labels that drift. The script does the
deterministic, multi-round-trip parts (capture the conversation URL, extract and
save the finished report); if it can't find the Deep Research toggle or a plan
button, the agent can do that one click itself via the chrome MCP and re-run
with `--assume-mode`.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import time
from pathlib import Path
from typing import Any

_WEBCHAT_PATH = Path(__file__).resolve().parents[2] / "reference" / "webchat.py"
_spec = importlib.util.spec_from_file_location("webchat", _WEBCHAT_PATH)
if _spec is None or _spec.loader is None:
    raise RuntimeError(f"cannot load shared driver at {_WEBCHAT_PATH}")
webchat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(webchat)


# Per-site Deep Research knobs. Buttons are matched by VISIBLE TEXT (via
# webchat.click_by_text), which survives redesigns far better than CSS selectors
# — the labels here are what the agent would read on screen.
DR: dict[str, dict[str, Any]] = {
    "gemini": {
        # Deep Research lives inside the composer's tools menu: open the menu
        # (its trigger has no text, matched via aria-label), then pick the item.
        "enable_path": [["Upload & tools"], ["Deep research", "Deep Research"]],
        "plan_texts": ["Start research", "Begin research"],
        # Panel ONLY — no chat-transcript fallback. The plan bubble lives in the
        # chat and is stable text, so a transcript fallback could read a long
        # plan as a finished "report"; the immersive panel exists only once
        # research actually produced output.
        "report_selectors": ["deep-research-immersive-panel"],
        "min_report_chars": 1200,
    },
    "chatgpt": {
        # Deep research is a radio item inside the composer's plus menu.
        "enable_path": [["Add files and more"], ["Deep research", "Deep Research"]],
        "plan_texts": ["Start research"],
        # The report renders inside a sandboxed frame the page DOM cannot read
        # (zero assistant messages in the main document on a finished run) —
        # check fetches it through the widget's own Export-to-Markdown control
        # instead of CSS report selectors.
        "report_export": True,
        "min_report_chars": 1200,
    },
    "perplexity": {
        # The mode selector is a composer menu whose trigger label is the CURRENT
        # mode ("Search" by default, "Deep research" once selected) — listing both
        # as the first step opens the menu from either state; the second step picks
        # the Deep research item. Research begins on submit (no plan-approval or
        # clarifying gate), so there is no plan_texts.
        "enable_path": [["Search", "Deep research"], ["Deep research"]],
        # The finished report renders into the same markdown-content-<n> block a
        # normal answer uses (just longer, with inline citations). Done = the stop
        # button is gone and the text has stopped growing — check's stability loop
        # handles it; no report_export / sandboxed frame here.
        "report_selectors": ['[id^="markdown-content"]'],
        "min_report_chars": 1200,
    },
}

# Seconds between the two report samples `check` compares to judge "done": a
# finished report is identical across the gap, an in-progress one still grows.
_STABILITY_GAP_S = 6.0


def _cfg(site: str) -> dict[str, Any]:
    try:
        return DR[site]
    except KeyError:
        raise ValueError(f"deep-research supports {', '.join(DR)}; got {site!r}") from None


def _slug(text: str, *, max_len: int = 40) -> str:
    words = re.findall(r"[A-Za-z0-9]+", text.lower())
    return "-".join(words)[:max_len].strip("-") or "research"


def _wait_for_conversation_url(site: str, *, timeout: float) -> str:
    """Poll until the page URL becomes a per-conversation URL (it changes once the
    first message is sent), so `check` has a stable handle to come back to."""
    base = webchat.site(site)["new_chat_url"].rstrip("/")
    deadline = time.monotonic() + timeout
    while True:
        url = webchat.current_url()
        if url.rstrip("/") != base and re.search(r"/[A-Za-z0-9_-]{6,}", url.removeprefix(base)):
            return url
        if time.monotonic() >= deadline:
            return webchat.current_url()
        time.sleep(1.0)


def _try_approve_plan(site: str, *, timeout: float) -> str | None:
    """If a research-plan confirmation is showing, click it. Returns the button
    text clicked, or None if no plan button appeared within `timeout`."""
    cfg = _cfg(site)
    plan_texts = cfg.get("plan_texts")
    if not plan_texts:
        # Some sites (Perplexity) begin researching the moment the prompt is
        # submitted — there is no plan-approval button to click.
        return None
    deadline = time.monotonic() + timeout
    while True:
        hit = webchat.click_by_text(plan_texts)
        if hit:
            return hit
        if time.monotonic() >= deadline:
            return None
        time.sleep(1.0)


def _read_report(site: str) -> str:
    cfg = _cfg(site)
    js = (
        "() => {"
        f"  for (const s of {json.dumps(cfg['report_selectors'])}) {{"
        "    const els = document.querySelectorAll(s);"
        "    if (els.length) return els[els.length - 1].innerText || '';"
        "  }"
        "  return '';"
        "}"
    )
    return webchat.evaluate(js)


def _save(site: str, url: str, report: str) -> Path:
    outdir = (
        webchat.downloads_root("deep-research") / f"{webchat.now_stamp()}-{site}-{_slug(report)}"
    )
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "report.md").write_text(report, encoding="utf-8")
    (outdir / "meta.json").write_text(
        json.dumps({"site": site, "url": url, "chars": len(report)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return outdir


def cmd_start(args: argparse.Namespace) -> dict[str, Any]:
    cfg = _cfg(args.site)
    # The research runs server-side once the plan is approved below, so this tab
    # is closed on the way out; `url` is the handle `check` reopens to poll.
    page_id = webchat.open_chat(args.site)
    try:
        enabled = True
        if not args.assume_mode:
            # Each step is one click (open a menu, then pick the item); every step
            # must land or the mode is not on.
            for step in cfg["enable_path"]:
                hit = webchat.click_by_text(step)
                if not hit:
                    raise RuntimeError(
                        f"could not find {step!r} while enabling Deep Research for "
                        f"{webchat.site(args.site)['label']} — enable it in the browser and "
                        f"re-run with --assume-mode (or update enable_path in research.py)"
                    )
                # A menu/mode click re-renders the composer area; let it settle.
                time.sleep(1.0)
        webchat.inject_prompt(args.site, args.prompt)
        via = webchat.submit(args.site)
        url = _wait_for_conversation_url(args.site, timeout=25.0)
        approved = _try_approve_plan(args.site, timeout=args.plan_wait)
        return {
            "site": args.site,
            "url": url,
            "submitted_via": via,
            "deep_research_enabled": enabled,
            "plan_approved": approved,
            "state": "running" if approved else "started",
        }
    finally:
        webchat.close_tab(page_id)


def _check_via_export(args: argparse.Namespace) -> dict[str, Any]:
    """check for a site whose report lives in a sandboxed frame: judge state
    from the rendered accessibility tree (which crosses frames) and fetch the
    finished report through the widget's own Export-to-Markdown control."""
    page_id = webchat.navigate(args.url)
    try:
        # The research widget is a separate frame that mounts well after the page
        # (tens of seconds on a finished run) — poll the rendered tree for a
        # definitive widget state instead of a fixed settle.
        deadline = time.monotonic() + 45.0
        snap = ""
        reloaded = False
        while time.monotonic() < deadline:
            time.sleep(3.0)
            snap = webchat.page_snapshot()
            if "Stop research" in snap:
                return {"state": "running", "url": args.url, "chars": 0}
            if "Export" in snap:
                break
            if "Error loading app" in snap and not reloaded:
                # The widget frame failed to mount (observed transiently on a
                # finished run); one reload recovers it.
                reloaded = True
                webchat.close_tab(page_id)  # the reload opens a fresh tab
                page_id = webchat.navigate(args.url)
        if "Export" not in snap:
            # Neither running nor finished: the research hasn't started — a plan
            # waiting for approval, or a clarifying question waiting for `reply`.
            approved = _try_approve_plan(args.site, timeout=3.0)
            return {
                "state": "running",
                "url": args.url,
                "approved_plan_this_check": approved,
                "chars": 0,
                "last_message": webchat.read_answer(args.site)[:1000],
            }
        outdir = (
            webchat.downloads_root("deep-research") / f"{webchat.now_stamp()}-{args.site}-report"
        )
        if not webchat.click_by_text(["Export"]):
            raise RuntimeError("finished report detected but the Export control would not open")
        time.sleep(1.0)
        dest = webchat.download_by_click(
            ["Export to Markdown"], dest=outdir / "report.md", suffixes=(".md",)
        )
        if dest is None:
            raise RuntimeError(
                "Export menu opened but 'Export to Markdown' did not produce a download "
                "(label drifted? check the widget's Export menu)"
            )
        report = dest.read_text(encoding="utf-8")
        (outdir / "meta.json").write_text(
            json.dumps(
                {"site": args.site, "url": args.url, "chars": len(report)},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return {
            "state": "done",
            "url": args.url,
            "chars": len(report),
            "dir": str(outdir),
            "report": report,
        }
    finally:
        webchat.close_tab(page_id)


def cmd_check(args: argparse.Namespace) -> dict[str, Any]:
    cfg = _cfg(args.site)
    webchat.assert_same_site(args.url, args.site)
    if cfg.get("report_export"):
        return _check_via_export(args)
    # check reopens the conversation in its own tab and closes it on the way out;
    # the research keeps running server-side, polled again via `url`.
    page_id = webchat.navigate(args.url)
    try:
        # The report surface mounts lazily after navigation (Gemini's immersive
        # panel takes 10-20s to auto-open on a finished conversation) — poll for it
        # rather than a fixed settle, or a finished report reads as absent.
        deadline = time.monotonic() + 30.0
        report = ""
        while True:
            report = _read_report(args.site)
            if report.strip() or time.monotonic() >= deadline:
                break
            time.sleep(2.0)
        if not report.strip():
            # No report output yet. This is the ONLY state where check clicks
            # anything: a plan still waiting for confirmation renders before any
            # report exists. (The plan-confirmation widget stays in the transcript
            # forever — clicking it on a page that already has a report would
            # inject a junk message into the conversation, so never click then.)
            approved = _try_approve_plan(args.site, timeout=3.0)
            return {
                "state": "running",
                "url": args.url,
                "approved_plan_this_check": approved,
                "chars": 0,
                # The last chat message: when a site asks clarifying questions
                # before starting, this is where the agent reads them (answer with
                # the `reply` command).
                "last_message": webchat.read_answer(args.site)[:1000],
            }
        # A finished report stops growing; an in-progress one is still changing.
        # Sample until two consecutive reads are identical (the panel hydrates in
        # chunks after mount, so a single fixed-gap pair reads a half-hydrated
        # finished report as "still changing"). No convergence inside the budget =
        # research genuinely still writing -> running. Stability (not a body-text
        # keyword scan, which the finished report's own prose would trip) is the
        # terminal signal, so check can't loop forever once research stops.
        budget = time.monotonic() + 60.0
        report2 = report
        stable = False
        while time.monotonic() < budget:
            time.sleep(_STABILITY_GAP_S)
            cur = _read_report(args.site)
            if cur == report2 and len(cur) >= cfg["min_report_chars"]:
                stable = True
                break
            report2 = cur
        if stable and not webchat.is_streaming(args.site):
            outdir = _save(args.site, args.url, report2)
            return {
                "state": "done",
                "url": args.url,
                "chars": len(report2),
                "dir": str(outdir),
                "report": report2,
            }
        return {
            "state": "running",
            "url": args.url,
            "approved_plan_this_check": None,
            "chars": len(report2),
        }
    finally:
        webchat.close_tab(page_id)


def cmd_reply(args: argparse.Namespace) -> dict[str, Any]:
    """Send a follow-up message into the research conversation — answering the
    clarifying questions some sites ask before starting. Returns right after
    the submit; the research output is still collected via `check`."""
    r = webchat.ask(args.site, args.prompt, conversation_url=args.url, wait=False)
    return {
        "site": args.site,
        "url": args.url,
        "submitted_via": r["submitted_via"],
        "state": "replied",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Gemini / ChatGPT / Perplexity Deep Research and fetch the report."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser(
        "start", help="enable Deep Research, submit a prompt, approve the plan"
    )
    p_start.add_argument("--site", required=True, choices=sorted(DR))
    p_start.add_argument("--prompt", required=True, help="the research question")
    p_start.add_argument(
        "--assume-mode",
        action="store_true",
        help="skip enabling Deep Research mode (you already turned it on in the browser)",
    )
    p_start.add_argument(
        "--plan-wait",
        type=float,
        default=60.0,
        help="seconds to wait for a research-plan button to auto-approve (default 60)",
    )
    p_start.set_defaults(fn=cmd_start)

    p_check = sub.add_parser("check", help="poll a started research; save the report when done")
    p_check.add_argument("--site", required=True, choices=sorted(DR))
    p_check.add_argument("--url", required=True, help="the conversation URL returned by start")
    p_check.set_defaults(fn=cmd_check)

    p_reply = sub.add_parser(
        "reply",
        help="answer a clarifying question the site asked before starting the research",
    )
    p_reply.add_argument("--site", required=True, choices=sorted(DR))
    p_reply.add_argument("--url", required=True, help="the conversation URL from start")
    p_reply.add_argument("--prompt", required=True, help="the answer to send")
    p_reply.set_defaults(fn=cmd_reply)

    args = parser.parse_args()
    result = args.fn(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
