---
name: web-ai
description: "Drive the AI web apps the user already pays for (ChatGPT/Gemini/Claude, plus Perplexity for web-search-grounded answers) through the logged-in browser. Children: console (multi-model Q&A), deep-research, media (image/video). Use for a hard question worth a second model, a question needing live web search, or a web-only capability, with no API credits spent."
---

# web-ai — drive the frontier-model web apps through the logged-in browser

The user pays a flat monthly fee for ChatGPT, Gemini, and Claude, but Ava's own
backbone plus metered APIs cost credits per call. This family closes that gap:
it drives those web UIs in the user's **logged-in Chrome** (the `chrome` MCP /
`ava.mcps.chrome`, same browser the `web-sources` adapters use), turning the
flat-rate subscriptions into capabilities Ava can call for free — and reaching
features that have **no API at all** (Deep Research, image/video generation).

**Pick a child, then read its SKILL.md** (`ava.help(ava.skills.web_ai.<child>)`).

| child | what it does | when |
|---|---|---|
| `console` | ask the same question to ChatGPT + Gemini + Claude (+ Perplexity, opt-in, for web-search-grounded answers), collect all answers | a hard/uncertain question (tricky math, edge-case reasoning) where a 2nd/3rd frontier opinion is worth it, or one needing live web search |
| `deep-research` | run Gemini / ChatGPT / Perplexity Deep Research, poll until the report is ready | a question that wants a long, cited, multi-source report (Perplexity has no plan/clarifying gate and tends to finish fastest) |
| `media` | generate an image (ChatGPT or Gemini) or video (Gemini) | the user asks for a generated image or video |

## How it works (shared by every child)

Each child is a self-contained CLI the agent runs with bash; it drives the
browser itself and prints JSON to stdout, mirroring the `web-sources` adapters.
The common mechanic — open a fresh chat, type the prompt, submit, wait for the
streamed answer to finish — lives once in `reference/webchat.py`, which the
children load with importlib. Messages can also carry a **local file
attachment** (`console --file`, e.g. hand a PDF or image to the flat-rate seat
for analysis) and **continue an existing conversation** (`console
--continue-url`, `deep-research reply`) instead of always opening a fresh one.

```bash
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-ai/<child>/reference/<entry>.py <args>
```

These calls run for **tens of seconds to minutes** (they wait on a streamed
answer or a rendered asset). When you launch one with `ava.shell.run`, pass a
generous `timeout=` — e.g. `ava.shell.run(".venv/bin/python ...", timeout=600)` —
or it hits the default 30s timeout and raises before the result is ready.

## Prerequisites

1. **The shared headed Chrome is logged in to the sites you call.** The user
   logs in once (all three accept "Continue with Google"); the session persists
   in the browser profile. If a child raises "composer not found", the site
   served a sign-in wall — open it in the shared browser and log in.
2. Runs on a machine with the `chrome` MCP and a headed Chrome on CDP, like the
   login-gated `web-sources` adapters.

## Completion is detected by text-stability, not a per-site spinner

Every site marks "still generating" with its own stop-button selector, and those
drift. So a child treats an answer as done when its text stops growing for a few
seconds **and** no known stop-button is visible — if the stop selector has
drifted, text-stability alone still converges. The only parts that must be
right are the composer selector (to type into) and the answer read (a selector
list, or a per-site `answer_js` override where the rendered DOM is not a
faithful text source — Claude reads its screen-reader mirror); both live in
`SITES` in `webchat.py`, easy to fix.

## When a site's UI drifts

These web UIs change without notice. When a child raises "composer not found" /
"prompt did not register" / "answer empty", the fix is almost always one drifted
CSS selector: open the site in the shared browser, inspect the live composer /
send button / answer node, and update the relevant list in `SITES`
(`$AVA_HOME/skills/web-ai/reference/webchat.py`). Selectors are tried in order, so prepend
the new one and keep the old as a fallback.

## Boundaries

- **Automating these sites is against their terms of service.** This is for the
  user's own single-seat, human-paced use of their own subscriptions, not scale.
- **ChatGPT sits behind Cloudflare.** A bot-check challenge can interrupt a run;
  if a ChatGPT call stalls, the user may need to clear a challenge in the
  browser once.
- Long jobs (Deep Research, video) are not awaited in one turn — the child
  starts the job, and the agent polls back later via `ava.watcher.at`. See the
  child's SKILL.md.
