---
name: console
description: Asks ChatGPT, Gemini, Claude, and optionally Perplexity the same question and collects their answers. Use for hard or uncertain reasoning, tricky math, edge cases, second opinions, or current questions needing live web search.
---

# console — a panel of frontier models for one question

Read [`../SKILL.md`](../SKILL.md) first for the shared model (logged-in browser,
selector drift). This child asks **one question to several models in parallel
conversations** and brings back all their answers for you to compare and
synthesize. It spends no API credits — it uses the user's flat-rate web
subscriptions.

## When to reach for it

When a question is hard enough that one model's answer isn't trustworthy on its
own: tricky math or proofs, subtle reasoning with easy-to-miss edge cases, a
design call where you want diverse takes, or fact-checking by cross-reading. If
the question is easy, just answer it — don't convene the panel.

## Usage (the agent runs bash)

```bash
# Ask all three (ChatGPT + Gemini + Claude):
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-ai/console/reference/ask.py --prompt "Prove that ..."

# A subset:
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-ai/console/reference/ask.py --models chatgpt,gemini --prompt "..."

# Perplexity — web-search-grounded, the best pick for current facts / cited
# sources (opt-in: not in the default three, since it answers from a live
# search rather than the model's own reasoning):
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-ai/console/reference/ask.py --models perplexity \
    --prompt "What changed in the latest <X> release? Cite sources."

# Long / multi-line question on stdin:
cat question.txt | $AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-ai/console/reference/ask.py

# Give a slow model more time (default 180s per model):
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-ai/console/reference/ask.py --timeout 300 --prompt "..."

# Attach a local file (PDF / image / ...) — analyzed on the flat-rate seat,
# no API credits. Sent to every model asked:
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-ai/console/reference/ask.py --models gemini \
    --file ~/Downloads/report.pdf --prompt "Summarize the attached report's key risks."

# Follow up in an existing conversation by chat ID (the `chat_id` from a
# previous result — preferred, it always points to the right conversation):
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-ai/console/reference/ask.py --models claude \
    --chat-id "<chat_id>" --prompt "Now check the edge case where ..."

# Follow up in an existing conversation (the `url` from a previous result).
# Exactly one model — a conversation belongs to one site:
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-ai/console/reference/ask.py --models claude \
    --continue-url "https://claude.ai/chat/<id>" --prompt "Now check the edge case where ..."

# Keep each model's browser tab open afterward (default closes them):
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-ai/console/reference/ask.py --keep-tab --prompt "..."
```

Each model answers in its own browser tab, which is **closed once the answer is
captured** (it's already in the JSON below) so tabs don't pile up in the shared
browser. The `url` in each result is the durable handle: resume that
conversation later with `--continue-url <url>`. Pass `--keep-tab` to leave the
tabs open instead.

## What it returns

JSON to stdout (and the same saved to
`~/Downloads/ava_<cluster>_web-ai/console/<stamp>-<slug>/` as `answers.md` +
`result.json`). Every result row includes a `chat_id` — use it with `--chat-id`
to continue the same conversation instead of starting a new one:

```
{
  "prompt": "...",
  "dir": "/Users/.../Downloads/ava_main_web-ai/console/20260609-...-prove-that",
  "results": [
    {"site":"chatgpt","label":"ChatGPT","ok":true,"complete":true,"chars":1840,"url":"https://chatgpt.com/c/...","chat_id":"abc-123","tab_kept":false,"answer":"..."},
    {"site":"gemini","label":"Gemini","ok":true,"complete":true,"chars":1520,"url":"...","chat_id":"xyz-456","tab_kept":false,"answer":"..."},
    {"site":"claude","label":"Claude","ok":false,"error":"RuntimeError: composer not found ..."}
  ]
}
```

One model failing (a sign-in wall, a drifted selector) does not sink the others
— it comes back as a row with `"ok": false` and an `error`. `complete: false`
means the answer didn't finish within the timeout; the partial text is still
returned.

**Your job after running it:** read the answers, note where they agree and
where they diverge, and give the user a synthesized result — not three raw
dumps. When they disagree on something checkable (a number, a proof step), that
disagreement is the signal to dig in.

## Troubleshooting

- `not logged in` → the auto-login attempt (Continue with Google) failed.
  Open the site in the shared browser and log in manually, then retry.
- `composer not found` / selector drift →
- `prompt did not register` / `answer empty` → a selector drifted; see the
  drift section in [`../SKILL.md`](../SKILL.md) and fix the one selector list in
  `webchat.py`.
- A ChatGPT run stalls → a Cloudflare bot-check may be up; clear it once in the
  browser.
