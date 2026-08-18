"""Shared driver for the web-ai skill family — talk to a chat-style AI web app
through the user's logged-in Chrome (the `chrome` MCP).

The web-ai children (console / deep-research / media) all do the same four
things to a frontier-model web UI: open a fresh chat, type a prompt into the
composer, submit it, and wait for the streamed answer to finish. That common
mechanic lives here so a selector fix happens in ONE place.

Everything is driven from inside this module via `ava.mcps.chrome` against the
shared headed Chrome on the user's logged-in session (the same browser the
web-sources adapters use). Nothing here is imported into the agent namespace;
the children load this file with importlib and call its functions, then print
JSON to stdout.

Why text-stability is the primary completion signal: every site marks "still
generating" differently (a stop button with a site-specific selector), and
those selectors drift. So `wait_until_idle` treats the answer as done when its
text stops growing for a few seconds AND no known stop-button is visible — if
the stop-button selector has drifted (matches nothing), text-stability alone
still converges. The selectors that MUST be right are only the composer (to
type) and the answer container (to read); both are in `SITES`, easy to fix.

Selectors are best-known as of authoring and WILL drift as the sites ship UI
changes. When a child raises "composer not found" / "answer empty", open the
site in the shared browser, inspect the live DOM, and update the one selector
list in `SITES`.

Per-site profiles and the leaf helpers live in sibling modules (_sites /
_dom / _utils); this file is the driver. Split 2026-08-07 (Task #1011) to
bring ava_builtins under the 800-line hard ceiling.
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from pathlib import Path
from typing import Any

import ava

# Sibling leaf modules live next to this file; it is loaded by path (children
# use importlib spec_from_file_location), so its own directory is put on
# sys.path for the plain absolute imports below.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _dom import (  # noqa: E402, F401  (re-exported for children/tests)
    _any_present_js,
    _resolve_clickable,
    _selected_page_id,
    click_by_text,
    download_by_click,
)
from _sites import SITES, site  # noqa: E402, F401
from _utils import (  # noqa: E402, F401
    _GROUP,
    _cluster,
    _new_idle_state,
    chat_id_from_url,
    chat_url,
    downloads_root,
    now_stamp,
)

# --------------------------------------------------------------------------- #
# chrome MCP plumbing. Each navigate opens a fresh tab this driver owns (so
# concurrent callers never clobber each other's or the user's pages); the
# inject / submit / read helpers then act on that just-selected tab.
# --------------------------------------------------------------------------- #

_JSON_OPEN = "```json"


def evaluate(js: str) -> Any:
    """Run JS in the shared browser's current page and return its parsed result.

    chrome-devtools-mcp wraps the return value in a ```json ... ``` block. Anchor
    on the FIRST opener and the LAST closing fence so backticks inside the
    returned content (code answers!) don't truncate the JSON. Raises if there is
    no fenced block (the script threw, or no page is selected).
    """

    resp = ava.mcps.chrome.evaluate_script(function=js)
    start = resp.find(_JSON_OPEN)
    end = resp.rfind("```")
    if start < 0 or end <= start + len(_JSON_OPEN):
        raise RuntimeError(f"evaluate_script returned no JSON block: {resp[:300]!r}")
    return json.loads(resp[start + len(_JSON_OPEN) : end])


def navigate(url: str) -> int | None:
    """Open `url` in a FRESH tab this driver owns, never reusing an existing page.

    new_page opens a new tab, selects it, and loads `url` in one step -- so every
    navigation lands on a tab created right here, never the page another caller
    (or the user) happens to have open. The inject / submit / read helpers below
    then act on this just-selected tab. That is what keeps concurrent callers
    from clobbering each other's (and the user's) tabs in the shared browser:
    each navigation starts its own tab instead of steering whatever page is
    currently selected.

    Returns the new tab's page id (for a later close_tab), or None if the
    page-list dump did not mark a selected tab.
    """

    return _selected_page_id(ava.mcps.chrome.new_page(url=url))


def close_tab(page_id: int | None) -> None:
    """Close the tab `navigate` opened, by its page id -- the one-shot cleanup so
    a quick ask does not leave its tab piling up in the shared browser.

    Only ever closes the id handed in (a tab this driver created), so the user's
    own tabs are never touched. No-ops on a None id, and swallows the close error
    when it is the last open tab (the browser refuses to close the final one) or
    the tab is already gone -- cleanup must not turn into a failure.
    """
    if page_id is None:
        return

    with contextlib.suppress(Exception):
        ava.mcps.chrome.close_page(pageId=page_id)


def select(page_id: int) -> None:
    """Make `page_id` the active tab so the read/inject helpers act on it.

    The shared browser has one active tab and `evaluate` runs on whichever it is,
    so a caller polling several owned tabs at once (`wait_many_idle`) selects each
    one right before reading it -- that is what keeps concurrent conversations
    from reading each other's page.
    """

    ava.mcps.chrome.select_page(pageId=page_id)


def current_url() -> str:
    """The shared browser's current page URL (`location.href`)."""
    return evaluate("() => location.href")


def assert_same_site(url: str, name: str) -> None:
    """Refuse to navigate the logged-in session to a URL whose host is not this
    site's host. The check paths take a stored conversation URL; this keeps a
    bad/attacker-supplied URL from reaching the logged-in browser."""
    host = site(name)["host"]
    if not (url.startswith(f"https://{host}/") or url == f"https://{host}"):
        raise ValueError(f"refusing to navigate to non-{host} URL: {url!r}")


def wait_until(predicate_js: str, *, timeout: float, poll: float = 0.5) -> bool:
    """Poll a JS expression until it returns truthy, or until `timeout`. Returns
    whether it became truthy."""
    deadline = time.monotonic() + timeout
    while True:
        if evaluate(predicate_js):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


# --------------------------------------------------------------------------- #
# Composer: open / inject / submit
# --------------------------------------------------------------------------- #


def _wait_composer(
    name: str, *, where: str, ready_timeout: float = 30.0, settle: float = 0.4
) -> None:
    """Wait for the composer to render on the current page.

    Raises if it never appears — that means not logged in (the site served a
    sign-in wall) or the composer selector drifted.
    """
    prof = site(name)
    if not wait_until(_any_present_js(prof["composer"]), timeout=ready_timeout):
        raise RuntimeError(
            f"{prof['label']}: composer not found at {where} "
            f"— not logged in, or the composer selector drifted (update SITES['{name}']['composer'])"
        )
    # The editor's JS handlers attach a beat after the node renders; a short
    # settle avoids injecting before the contenteditable is wired up.
    time.sleep(settle)


def open_chat(
    name: str,
    *,
    ready_timeout: float = 30.0,
    settle: float = 0.4,
    login_timeout: float = 15.0,
) -> int | None:
    """Open the site's fresh-chat URL in a new owned tab and wait for the composer.

    When no composer appears within a few seconds, checks for a login wall
    (``login_indicators`` text in the page snapshot) and walks ``auto_login``
    click paths to attempt a Google-OAuth sign-in. Raises ``RuntimeError``
    when every path is exhausted without reaching the composer.

    Returns the new tab's page id (for a later close_tab), or None if unmarked.
    """
    prof = site(name)
    page_id = navigate(prof["new_chat_url"])
    time.sleep(2.5)

    if not wait_until(_any_present_js(prof["composer"]), timeout=ready_timeout):
        # Composer not found — check if this is a login wall and try auto-login.
        snapshot = page_snapshot().lower()
        is_login_wall = any(ind.lower() in snapshot for ind in prof.get("login_indicators", []))
        if is_login_wall:
            for path in prof.get("auto_login", []):
                for step in path:
                    if not click_by_text([step]):
                        break
                    time.sleep(1.5)
                else:
                    if wait_until(_any_present_js(prof["composer"]), timeout=login_timeout):
                        break
            else:
                close_tab(page_id)
                raise RuntimeError(
                    f"{prof['label']}: not logged in. Please open {prof['new_chat_url']} "
                    f"in the shared browser, log in (Continue with Google), then retry."
                )
        else:
            # No login indicators — it's selector drift, not a login wall.
            close_tab(page_id)
            raise RuntimeError(
                f"{prof['label']}: composer not found at {prof['new_chat_url']} "
                f"— not logged in, or the composer selector drifted "
                f"(update SITES['{name}']['composer'])"
            )

    # The editor's JS handlers attach a beat after the node renders; a short
    # settle avoids injecting before the contenteditable is wired up.
    time.sleep(settle)
    return page_id


def inject_prompt(name: str, text: str) -> None:
    """Type `text` into the composer, replacing whatever is there.

    Uses the native value setter for <textarea>/<input> and `execCommand
    insertText` for contenteditable — both fire the input events the site's
    editor (React/ProseMirror/Quill) listens to, unlike setting `.value` /
    `.textContent` directly. Reads the composer back and raises if nothing
    registered (editor rejected the programmatic insert, or selector drift).
    """
    prof = site(name)
    js = (
        "() => {"
        f"  const sels = {json.dumps(prof['composer'])};"
        f"  const text = {json.dumps(text)};"
        "  let el = null;"
        "  for (const s of sels) { el = document.querySelector(s); if (el) break; }"
        "  if (!el) return { ok:false, reason:'composer-not-found' };"
        "  el.focus();"
        "  const tag = el.tagName.toLowerCase();"
        "  if (tag === 'textarea' || tag === 'input') {"
        "    const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;"
        "    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;"
        "    setter.call(el, text);"
        "    el.dispatchEvent(new Event('input', { bubbles: true }));"
        "  } else {"
        "    const sel = window.getSelection();"
        "    sel.selectAllChildren(el);"
        "    document.execCommand('insertText', false, text);"
        "  }"
        "  const got = (el.innerText !== undefined ? el.innerText : el.value) || '';"
        "  return { ok:true, got_len: got.length };"
        "}"
    )
    res = evaluate(js)
    if not res.get("ok"):
        raise RuntimeError(f"{prof['label']}: failed to inject prompt ({res.get('reason')})")
    if not res.get("got_len"):
        raise RuntimeError(
            f"{prof['label']}: prompt did not register in the composer "
            f"— the editor rejected the programmatic insert, or the composer selector drifted"
        )


def submit(name: str) -> str:
    """Send the composed prompt. Clicks the site's send button if present and
    enabled; otherwise dispatches an Enter keystroke on the composer. Returns a
    short tag of which path fired. Raises if neither target exists."""
    prof = site(name)
    js = (
        "() => {"
        f"  for (const s of {json.dumps(prof['send'])}) {{"
        "    const b = document.querySelector(s);"
        "    if (b && !b.disabled) { b.click(); return { ok:true, via:'button:'+s }; }"
        "  }"
        f"  for (const s of {json.dumps(prof['composer'])}) {{"
        "    const el = document.querySelector(s);"
        "    if (el) {"
        "      el.focus();"
        "      for (const t of ['keydown','keypress','keyup']) {"
        "        el.dispatchEvent(new KeyboardEvent(t, {key:'Enter', code:'Enter', keyCode:13, which:13, bubbles:true, cancelable:true}));"
        "      }"
        "      return { ok:true, via:'enter' };"
        "    }"
        "  }"
        "  return { ok:false, reason:'no-send-target' };"
        "}"
    )
    res = evaluate(js)
    if not res.get("ok"):
        raise RuntimeError(f"{prof['label']}: could not submit ({res.get('reason')})")
    return res["via"]


# --------------------------------------------------------------------------- #
# Answer: detect streaming, read text, wait for completion
# --------------------------------------------------------------------------- #


def is_streaming(name: str) -> bool:
    """Whether a known stop/streaming control is currently visible. A drifted
    stop selector matches nothing and returns False — the safe direction, since
    `wait_until_idle` then falls back to pure text-stability."""
    prof = site(name)
    return bool(evaluate(_any_present_js(prof["stop"])))


def read_answer(name: str) -> str:
    """Text of the LAST assistant message on the page, or '' if none yet.

    A site profile carries exactly one of two read strategies: `answer` (a
    selector list — innerText of the last match wins) or `answer_js` (a full JS
    override for sites whose rendered DOM is not a faithful text source).
    """
    prof = site(name)
    if "answer_js" in prof:
        return evaluate(prof["answer_js"])
    js = (
        "() => {"
        f"  for (const s of {json.dumps(prof['answer'])}) {{"
        "    const els = document.querySelectorAll(s);"
        "    if (els.length) return els[els.length - 1].innerText || '';"
        "  }"
        "  return '';"
        "}"
    )
    return evaluate(js)


def _idle_step(name: str, st: dict[str, Any], *, now: float, stable_secs: float) -> None:
    """Advance the idle detector one poll for the currently-selected tab.

    Reads the tab's last answer + streaming flag and folds them into `st`. Marks
    `st['done']` (with `answer` / `complete`) once the answer is non-empty,
    unchanged for `stable_secs`, not streaming, AND a real answer actually
    streamed in (the stop button was seen, or the text grew more than once) --
    that last guard stops a static pre-first-token placeholder from being read as
    a finished answer.
    """
    text = read_answer(name)
    streaming = is_streaming(name)
    st["saw_stream"] = st["saw_stream"] or streaming
    if text != st["last"]:
        if len(text) > len(st["last"]):
            st["growth_steps"] += 1
        st["last"] = text
        st["stable_start"] = None
    elif text and not streaming and (st["saw_stream"] or st["growth_steps"] >= 2):
        if st["stable_start"] is None:
            st["stable_start"] = now
        elif now - st["stable_start"] >= stable_secs:
            st["answer"] = text
            st["complete"] = True
            st["done"] = True


def wait_until_idle(
    name: str, *, timeout: float, stable_secs: float = 3.5, poll: float = 0.7
) -> dict[str, Any]:
    """Wait for the streamed answer to finish on the current page.

    Done = the last assistant message is non-empty, has not changed for
    `stable_secs`, no stop-button is visible, AND a real answer actually streamed
    in (see `_idle_step`). Returns `{"answer": str, "complete": bool}`; `complete`
    is False on timeout (returns the best partial answer captured).
    """
    deadline = time.monotonic() + timeout
    st = _new_idle_state()
    while True:
        _idle_step(name, st, now=time.monotonic(), stable_secs=stable_secs)
        if st["done"]:
            return {"answer": st["answer"], "complete": True}
        if time.monotonic() >= deadline:
            return {"answer": st["last"], "complete": False}
        time.sleep(poll)


def wait_many_idle(
    tabs: dict[Any, dict[str, Any]],
    *,
    timeout: float,
    stable_secs: float = 3.5,
    poll: float = 0.7,
) -> dict[Any, dict[str, Any]]:
    """Wait for several already-open tabs' answers to finish, polled round-robin.

    `tabs` maps a caller key -> {'name': site, 'page_id': owned tab id}. The
    shared browser has one active tab, so each tab is `select`ed right before it
    is read; that is what lets independent conversations be polled together
    without clobbering. Loops until every tab settles or the shared `timeout`
    elapses -- overlapping the slow part (waiting for each streamed answer)
    instead of waiting them out one at a time.

    Returns key -> {'answer': str, 'complete': bool}; `complete` is False for a
    tab that did not settle in time (its best partial answer is returned).
    """
    if not tabs:
        return {}
    deadline = time.monotonic() + timeout
    states = {key: _new_idle_state() for key in tabs}
    while True:
        for key, tab in tabs.items():
            st = states[key]
            if st["done"]:
                continue
            select(tab["page_id"])
            _idle_step(tab["name"], st, now=time.monotonic(), stable_secs=stable_secs)
        if all(states[key]["done"] for key in tabs):
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(poll)
    return {
        key: {"answer": st["answer"] if st["done"] else st["last"], "complete": st["complete"]}
        for key, st in states.items()
    }


def ask(
    name: str,
    prompt: str,
    *,
    timeout: float = 180.0,
    file: str | Path | None = None,
    conversation_url: str | None = None,
    wait: bool = True,
    keep_tab: bool = False,
) -> dict[str, Any]:
    """Send `prompt` and return the finished answer.

    By default opens a fresh chat; with `conversation_url` it continues that
    existing conversation instead (a follow-up turn). `file` attaches a local
    file to the message through the site's own attach control. With wait=False
    it returns right after the submit with an empty answer — for messages whose
    response is collected later (answering a long job's clarifying question).

    Tab lifecycle: each ask opens its own tab. Once the answer is collected the
    tab is closed (one-shot — nothing left piling up in the shared browser); the
    returned `url` is the durable handle to resume the conversation later (pass
    it back as `conversation_url`, which re-opens it in a fresh tab). `keep_tab`
    leaves the tab open instead — for when you want to keep watching or working
    in it. wait=False always keeps the tab (the answer is still streaming).

    Returns `{site, label, submitted_via, answer, complete, url, chat_id, tab_kept}`.
    `complete` is False if the answer did not stabilize within `timeout` (always
    False when wait=False). `tab_kept` says whether the tab was left open.
    `chat_id` is the stable conversation identifier for follow-ups.
    """
    prof = site(name)
    page_id, via = _start(name, prompt, file=file, conversation_url=conversation_url)
    keep = False
    try:
        if not wait:
            # The answer is still streaming and gets collected later via `url`, so
            # the tab must stay open regardless of keep_tab.
            keep = True
            url = current_url()
            return {
                "site": name,
                "label": prof["label"],
                "submitted_via": via,
                "answer": "",
                "complete": False,
                "url": url,
                "chat_id": chat_id_from_url(name, url),
                "tab_kept": True,
            }
        result = wait_until_idle(name, timeout=timeout)
        # Read the url BEFORE any close — closing moves the selection off this tab.
        url = current_url()
        keep = keep_tab
        return {
            "site": name,
            "label": prof["label"],
            "submitted_via": via,
            "answer": result["answer"],
            "complete": result["complete"],
            "url": url,
            "chat_id": chat_id_from_url(name, url),
            "tab_kept": keep_tab,
        }
    finally:
        # Close the owned tab unless it is deliberately kept (wait=False streams
        # the answer for a later read; keep_tab leaves it open). The finally also
        # closes it when wait_until_idle raises, so an error never leaks the tab.
        if not keep:
            close_tab(page_id)


def _start(
    name: str,
    prompt: str,
    *,
    file: str | Path | None = None,
    conversation_url: str | None = None,
) -> tuple[int | None, str]:
    """Open the tab, type the prompt, attach any file, and submit it.

    The setup half shared by `ask` and `ask_many`: opens a fresh chat (or
    re-opens `conversation_url` to continue it), injects `prompt`, attaches `file`
    after typing, and submits. Returns the owned tab's page id (for a later
    select / close) and a short tag of how the submit fired.
    """
    if conversation_url is not None:
        assert_same_site(conversation_url, name)
        page_id = navigate(conversation_url)
        _wait_composer(name, where=conversation_url)
    else:
        page_id = open_chat(name)
    try:
        inject_prompt(name, prompt)
        if file is not None:
            # Attach AFTER typing: with text present, the send control's
            # disabled-state reflects only the upload, which attach_file waits out.
            attach_file(name, file)
        via = submit(name)
    except Exception:
        # The tab is ours and the submit never landed; close it so a setup
        # failure (selector drift, attach timeout) does not leave it piling up
        # in the shared browser. close_tab no-ops on a None id.
        close_tab(page_id)
        raise
    return page_id, via


def ask_many(
    specs: list[dict[str, Any]],
    *,
    timeout: float = 180.0,
    keep_tab: bool = False,
) -> list[dict[str, Any]]:
    """Ask several models concurrently over the shared browser; one row per spec.

    Each spec is `{'name': site, 'prompt': str, 'file'?: path, 'conversation_url'?:
    str}`. Setup (open tab, inject, submit) runs serially per spec -- it is fast,
    and each step opens/selects its OWN tab, so the specs do not clobber one
    another. A spec whose setup raises becomes an error row and is dropped from
    the wait set, so one dead model never sinks the panel. Every live tab is then
    polled round-robin until its answer settles or `timeout`, overlapping the slow
    streamed-answer wait. Tabs are closed afterward unless `keep_tab`.

    Each model's `timeout` budget runs concurrently (all tabs share one deadline
    measured from when polling starts), so the wall-clock is the slowest single
    answer, not the sum.

    Returns a list order-matching `specs`. A live row mirrors `ask`:
    `{site, label, submitted_via, answer, complete, url, tab_kept}`. A setup
    failure is `{site, label, error}`.
    """
    rows: dict[int, dict[str, Any]] = {}
    live: dict[int, dict[str, Any]] = {}
    for i, spec in enumerate(specs):
        name = spec["name"]
        prof = site(name)
        try:
            page_id, via = _start(
                name,
                spec["prompt"],
                file=spec.get("file"),
                conversation_url=spec.get("conversation_url"),
            )
        except Exception as exc:
            # One model's setup failure (sign-in wall, selector drift) is captured
            # as a row, never raised — it must not sink the rest of the panel.
            rows[i] = {
                "site": name,
                "label": prof["label"],
                "error": f"{type(exc).__name__}: {exc}",
            }
            continue
        if page_id is None:
            rows[i] = {
                "site": name,
                "label": prof["label"],
                "error": "RuntimeError: could not identify the opened tab (page-list format drift)",
            }
            continue
        live[i] = {"name": name, "label": prof["label"], "page_id": page_id, "via": via}

    closed: set[int] = set()
    try:
        waited = wait_many_idle(
            {i: {"name": v["name"], "page_id": v["page_id"]} for i, v in live.items()},
            timeout=timeout,
        )
        for i, v in live.items():
            # Select this tab before reading its url, and read it BEFORE any close
            # — closing moves the selection off the tab.
            select(v["page_id"])
            url = current_url()
            if not keep_tab:
                close_tab(v["page_id"])
                closed.add(v["page_id"])
            rows[i] = {
                "site": v["name"],
                "label": v["label"],
                "submitted_via": v["via"],
                "answer": waited[i]["answer"],
                "complete": waited[i]["complete"],
                "url": url,
                "chat_id": chat_id_from_url(v["name"], url),
                "tab_kept": keep_tab,
            }
    finally:
        # If the wait/collect raised partway, close any live tab not yet closed so
        # a mid-panel failure never leaks the rest into the shared browser.
        if not keep_tab:
            for v in live.values():
                if v["page_id"] not in closed:
                    close_tab(v["page_id"])
    return [rows[i] for i in range(len(specs))]


def page_snapshot() -> str:
    """The rendered accessibility tree of the current page, as text. Crosses
    frame boundaries the page DOM cannot (a widget rendered in a sandboxed
    frame still shows up here), so it doubles as the read path for content
    that CSS selectors cannot reach."""

    return ava.mcps.chrome.take_snapshot()


def attach_file(name: str, path: str | Path, *, upload_timeout: float = 90.0) -> None:
    """Attach a local file to the message being composed, via the site's own
    attach control. Walks `attach_path` (a click sequence whose FINAL control
    opens the OS file chooser — intercepted and fed `path`), then waits for the
    upload to finish by polling the send control's disabled state. Call after
    `inject_prompt` (with an empty composer the send control is disabled anyway
    and the upload wait could never distinguish the two).

    Raises if the file is missing, an attach control can't be found, or the
    upload doesn't finish within `upload_timeout`.
    """

    prof = site(name)
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"attachment not found: {p}")
    steps = prof["attach_path"]
    for step in steps[:-1]:
        if not click_by_text(step):
            raise RuntimeError(
                f"{prof['label']}: could not find {step!r} while attaching "
                f"(update SITES['{name}']['attach_path'])"
            )
        time.sleep(0.8)
    hit = _resolve_clickable(steps[-1])
    if hit is None:
        raise RuntimeError(
            f"{prof['label']}: could not find {steps[-1]!r} while attaching "
            f"(update SITES['{name}']['attach_path'])"
        )
    # img-src baseline BEFORE the upload: an image attachment registers as a
    # NEW thumbnail (often carrying no filename anywhere), a document as its
    # filename in text — the chip probe below accepts either.
    pre_imgs: list[str] = evaluate(
        "() => [...document.images].map((i) => i.currentSrc || i.src || '').filter(Boolean)"
    )
    ava.mcps.chrome.upload_file(uid=hit[0], filePath=str(p))
    # The chip proves the file registered. Some sites keep send enabled on text
    # alone, so the send-state probe alone would pass before the file landed;
    # the send probe after this covers the sites that DO disable it mid-upload.
    chip_visible = (
        "() => {"
        f"  const NAME = {json.dumps(p.name)};"
        f"  const PRE = new Set({json.dumps(pre_imgs)});"
        "  if ((document.body.innerText || '').includes(NAME)) return true;"
        "  if ([...document.querySelectorAll('[aria-label]')]"
        "      .some((e) => (e.getAttribute('aria-label') || '').includes(NAME))) return true;"
        "  return [...document.images].some((i) => {"
        "    const s = i.currentSrc || i.src || '';"
        "    return s && !PRE.has(s);"
        "  });"
        "}"
    )
    if not wait_until(chip_visible, timeout=upload_timeout, poll=1.0):
        raise RuntimeError(
            f"{prof['label']}: attachment {p.name!r} never appeared on the page "
            f"within {upload_timeout}s — the file chooser was not intercepted, or "
            f"the upload failed"
        )
    send_enabled = (
        "() => {"
        f"  for (const s of {json.dumps(prof['send'])}) {{"
        "    const b = document.querySelector(s);"
        "    if (b && !b.disabled && b.getAttribute('aria-disabled') !== 'true') return true;"
        "  }"
        "  return false;"
        "}"
    )
    if not wait_until(send_enabled, timeout=upload_timeout, poll=1.0):
        raise RuntimeError(
            f"{prof['label']}: attachment upload did not finish within {upload_timeout}s "
            f"(send control still disabled)"
        )


def check_login(name: str, *, ready_timeout: float = 8.0) -> bool:
    """Whether the site is currently logged-in in the shared browser.

    Opens a fresh tab to the site, waits for the composer to appear (up to
    ``ready_timeout``), then checks the accessibility snapshot for
    ``login_indicators``. Returns True when a composer is found (definitely
    logged in) or no known login indicator text is present. Returns False when
    login-wall text is seen and no composer is present.

    The tab is closed before returning so this is a non-invasive probe.
    """
    prof = site(name)
    page_id = navigate(prof["new_chat_url"])
    try:
        has_composer = wait_until(_any_present_js(prof["composer"]), timeout=ready_timeout)
        if has_composer:
            return True
        snapshot = page_snapshot().lower()
        for indicator in prof.get("login_indicators", []):
            if indicator.lower() in snapshot:
                return False
        # No composer but also no login text — ambiguous. Treat as not logged in
        # to be safe (will trigger ensure_login which is also safe).
        return False
    finally:
        close_tab(page_id)


def ensure_login(name: str) -> None:
    """Guarantee the site is logged-in, or raise with a clear message.

    Opens the site, checks for a login wall, and walks ``auto_login`` click
    paths to attempt a Google-OAuth sign-in.  Waits for the composer to appear
    after each attempt.  Raises ``RuntimeError`` when every auto-login path is
    exhausted without reaching the composer — the user must log in manually.

    The tab is left open on success so the caller can proceed to inject its
    prompt (the composer is already present).  The caller takes ownership of the
    returned page id for close.
    """
    prof = site(name)
    page_id = navigate(prof["new_chat_url"])
    time.sleep(2.5)

    # Already logged in?
    if evaluate(_any_present_js(prof["composer"])):
        return  # page_id is the caller's to use

    # Try each auto-login path
    for path in prof.get("auto_login", []):
        for step in path:
            if not click_by_text([step]):
                break  # this path failed, try the next
            time.sleep(1.5)
        else:
            # Path completed — wait for composer
            if wait_until(_any_present_js(prof["composer"]), timeout=15.0):
                return
        # Path failed; re-navigate to try the next
        close_tab(page_id)
        page_id = navigate(prof["new_chat_url"])
        time.sleep(2.5)

    close_tab(page_id)
    raise RuntimeError(
        f"{prof['label']}: not logged in. Please open {prof['new_chat_url']} in "
        f"the shared browser, log in (Continue with Google), then retry."
    )
