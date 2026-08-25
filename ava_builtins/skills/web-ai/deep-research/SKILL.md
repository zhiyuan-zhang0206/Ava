---
name: deep-research
description: "Runs Gemini, ChatGPT, or Perplexity Deep Research in a logged-in browser and retrieves the cited report. Use when the user wants long, multi-source, cited web research rather than a quick answer; expect a long-running job."
---

# deep-research — Gemini / ChatGPT / Perplexity Deep Research, fetched as a report

Read [`../SKILL.md`](../SKILL.md) first for the shared model. Deep Research runs
for minutes and may pause for plan approval, so it does **not** fit one turn.
The flow is: **start the job, then poll back later** — you (the agent) drive the
browser in your own turn, and `ava.watcher.at` just nudges you when it's time to
look again. No watcher process touches the browser (that would race the tab).

## The loop you run

```python
import ava, json, datetime as dt

# 1. Start it (this drives the browser: enable Deep Research, type, submit, approve plan)
out = json.loads(ava.shell.run(
    '$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-ai/deep-research/reference/research.py '
    'start --site gemini --prompt "How have small modular reactors progressed since 2023?"',
    timeout=180,  # start enables the mode, submits, and may wait out the plan step
))
url = out["url"]

# 2. Schedule yourself to look again in ~8 min, then idle (return no tool call)
ava.watcher.at(dt.timedelta(minutes=8), f"check deep research: gemini {url}", name="deep-research-poll")
```

When that reminder wakes you, run **check**; reschedule if it's still running,
deliver if it's done:

```python
res = json.loads(ava.shell.run(
    f'$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-ai/deep-research/reference/research.py check --site gemini --url "{url}"',
    timeout=60,
))

if res["state"] == "done":
    # res["dir"]/report.md is saved; res["report"] is the full text — synthesize for the user
    ...
else:  # "running"
    ava.watcher.at(dt.timedelta(minutes=6), f"check deep research: gemini {url}", name="deep-research-poll")
    # idle again
```

Deep Research typically finishes in ~5-15 minutes; poll every 6-8 minutes so you
don't thrash. Stop after a sensible number of rounds (e.g. ~6) and tell the user
if it never completes.

## Clarifying questions (mostly ChatGPT)

A site may ask clarifying questions in chat before starting the research. A
running `check` with `chars: 0` carries the conversation's last message in
`last_message` — if that is a question, answer it with `reply` and keep
polling. You can sidestep most of this by ending the research prompt with
"make reasonable assumptions yourself and start without asking me clarifying
questions".

Perplexity is the exception: it starts researching the moment you submit (no
plan, no clarifying gate — `start` returns `state: started` immediately) and
usually finishes faster (~1-3 min) than Gemini/ChatGPT.

## Commands

```bash
# Start (default site gemini). --assume-mode if you already enabled Deep Research
# in the browser yourself; --plan-wait N seconds to auto-approve the research plan.
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-ai/deep-research/reference/research.py start --site gemini --prompt "..."
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-ai/deep-research/reference/research.py start --site chatgpt --prompt "..." --assume-mode
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-ai/deep-research/reference/research.py start --site perplexity --prompt "..."

# Check / fetch (saves report.md + meta.json when done):
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-ai/deep-research/reference/research.py check --site gemini --url "<url>"

# Answer a clarifying question, then keep polling with check:
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-ai/deep-research/reference/research.py reply --site chatgpt --url "<url>" \
    --prompt "Global scope; prioritize 2024-2026 primary sources."
```

`start` prints `{site, url, state}` (state `started` or `running` if the plan was
auto-approved). `check` prints `{state:"running", ...}` or
`{state:"done", dir, chars, report, url}`. The report lands at
`~/Downloads/ava_<cluster>_web-ai/deep-research/<stamp>-<site>-<slug>/`.

## When the script can't drive a step

This is the most UI-fragile child. Two steps depend on labels that drift:

- **Enabling Deep Research mode** — if `start` raises "could not find the Deep
  Research toggle", turn it on yourself with the chrome MCP (click the Deep
  Research control), then re-run `start --assume-mode`.
- **Approving the research plan** — Gemini shows a plan and waits. `start` and
  `check` try to click "Start research" by its text; if the label changed, click
  it yourself, then keep polling with `check`.

The script always owns the deterministic, multi-round-trip parts: capturing the
conversation URL, judging done, and extracting + saving the finished report.

## Troubleshooting

- `composer not found` / sign-in wall → that site isn't logged in; log in once.
- Stuck in `running` forever → `check` calls a report "done" only when it is at
  least `min_report_chars`, unchanged across a short stability gap, and no
  stop-button is up. If a UI change broke `report_selectors` (the report reads as
  empty/short), it never settles; open the URL, confirm the report finished, and
  fix `report_selectors` / `min_report_chars` in `research.py`.
- ChatGPT stalls → a Cloudflare bot-check may be up; clear it once in the browser.
