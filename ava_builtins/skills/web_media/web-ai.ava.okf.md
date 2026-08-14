---
type: doc
title: web-ai skill — Drive cutting-edge model web apps via logged-in browser
description: "Drive the user's monthly-paid AI web apps (ChatGPT/Gemini/Claude + Perplexity) through an already logged-in Chrome, without spending API credits; also reach capabilities that have no API at all (Deep Research, image/video generation). Sub-skills: console / deep-research / media."
tags:
- extensions
- agent-instruction
---

# web-ai skill — Drive cutting-edge model web apps via logged-in browser

## What it is
Users pay a **monthly flat fee** for ChatGPT/Gemini/Claude, but Ava's own backbone plus metered APIs consume credits per call. This set of skills (`$AVA_HOME/skills/web-ai/`) fills this gap: drive those web UIs inside the user's **already logged-in Chrome** (the same Chrome MCP / `ava.mcps.chrome`, shared with the web-sources adapter), turning the flat-fee subscription into capabilities Ava can invoke for free — and reaching features that **have no API at all** (Deep Research, image/video generation).

## Three sub-skills
- **console** — ask the same question to ChatGPT + Gemini + Claude simultaneously (+ optional Perplexity for web search grounding), collect all answers; use for difficult / uncertain questions where a second or third cutting-edge opinion is valuable, or when live web search is needed.
- **deep-research** — run Deep Research on Gemini/ChatGPT/Perplexity, poll until the report is ready; use when a long, cited, multi-source report is needed.
- **media** — generate images (ChatGPT/Gemini) or video (Gemini) and download the files.

Each sub-skill is a self-contained CLI (agent runs via bash, prints JSON to stdout, mirroring the web-sources adapter); the shared "open new conversation → input → submit → wait for streaming answer to finish" mechanics are in `reference/webchat.py`.

## Key dependencies
- [[ava_builtins/skills/web_media/web_media.ava.okf.md|Web & multimodal skill]] — belongs to functional group
- [[ava/mcps.ava.okf.md|MCP integration]] — drive logged-in browser via [[ava_builtins/mcps/chrome/chrome.ava.okf.md|Chrome MCP]]
- [[ava_builtins/skills/web_media/web-sources.ava.okf.md|web-sources]] — same logged-in browser, same CLI+JSON form (fetch content vs drive model)
