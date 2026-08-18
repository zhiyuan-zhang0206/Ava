# web-ai — drive the frontier-model web apps the user already pays for

## Why this exists

The user holds flat-rate subscriptions to ChatGPT, Gemini, and Claude. Ava's own
backbone plus metered APIs cost credits per call, while those subscriptions are
already paid and sit idle to Ava. Driving the web UIs in the user's logged-in
Chrome (the `chrome` MCP, same browser the `web-sources` adapters use) closes
the gap two ways:

- **No API credits** for things Ava can do anyway (Q&A) — use the flat-rate web seat.
- **Web-only capabilities** that have no API at all — Deep Research, image/video.

The browser is the universal adapter; each capability is a self-contained CLI
skill (the "how-to" the agent runs); the model's own reasoning absorbs UI drift.
This is the same shape as `web-sources` (a root skill with per-target children),
pointed at AI apps instead of content sources.

## Shape

Root skill `ava_builtins/skills/web-ai/` with a shared driver `reference/webchat.py` (open a
fresh chat, type the prompt, submit, wait for the streamed answer to finish —
completion judged by text-stability so a drifted stop-button selector still
converges). Children load it with importlib and add their specifics.

**Tab ownership** (issue #1109). Each navigation opens its OWN new tab rather
than steering the currently-selected page, so concurrent callers never clobber
each other's or the user's tabs in the shared Chrome. A one-shot call (a console
answer, a finished media download) closes its tab when done; the returned `url`
is the durable handle to resume later (`--continue-url` / `check --url`).
`--keep-tab` keeps the tab open, and a still-running job always keeps its.

`console` exploits this: it sets up every model's tab (open, inject, submit)
serially — fast — then polls them all round-robin (`webchat.ask_many` /
`wait_many_idle`), selecting each tab right before it is read, so the streamed
answers overlap and the wall-clock is the slowest single answer, not the sum.
Round-robin rather than threads because the shared browser has one active tab and
`evaluate` runs on whichever it is — concurrent threads would fight over the
selection.

## Roadmap

- [x] `console` — ask one question to ChatGPT + Gemini + Claude, collect all
  answers for cross-reading. (PR1)
- [x] `deep-research` — Gemini / ChatGPT Deep Research: start the job, poll back
  via `ava.watcher.at` until the cited report is ready, save it. (PR2)
- [x] `media` — image (ChatGPT / Gemini) and video (Gemini) generation:
  submit the prompt, wait for the asset, download it to Downloads. (PR3)
- [x] file attachments (`console --file`) + conversation follow-ups
  (`console --continue-url`, `deep-research reply`).
- [x] `perplexity` as a console site — user call: its search is the best.
  Landed (the corp-gateway 502 blocking it was resolved); live in
  `ava_builtins/skills/web-ai/reference/_sites.py`.

Future candidates (not committed): Grok DeepSearch, AI Studio.

## Deliberately dropped

- **NotebookLM, Midjourney** — out of scope for now (user call, 2026-06-09).
- **Scale / multi-seat use** — this is single-seat, human-paced use of the
  user's own subscriptions. Automating these sites is against their ToS; that is
  acceptable for personal use, not for volume.

## Known caveats

- **ChatGPT is behind Cloudflare** — a bot-check can interrupt a run; the user
  clears it once in the browser.
- **Selectors drift** — the composer / send / answer / stop selectors live in
  one `SITES` table in `webchat.py` (its `_sites.py` sibling); a drifted site is a one-selector fix.
- **NOT tested end-to-end at authoring time** — the browser-driving paths were
  written against best-known DOM as of authoring but not exercised against the
  live logged-in sites (that would drive the user's real session). First real
  use is the live test; expect to fix a selector or two.
