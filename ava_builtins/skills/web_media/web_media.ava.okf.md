---
type: doc
title: Web & multimodal skill
description: A set of skills for fetching content from web & media — drive cutting-edge AI via logged-in browser, fetch content from any source, transcribe audio/video. All built into the core repo (origin=repo).
tags:
- extensions
- agent-instruction
---

# Web & multimodal skill

## What it is
A set of skills for fetching content from **web & multimodal media**: drive ChatGPT/Gemini/Claude/Perplexity via logged-in browser, fetch content from any source (including per-platform adapters), transcribe audio/video to text. All built into the core repo (origin=repo).

| Skill | Purpose | Details |
|------|------|------|
| web-ai | Drive ChatGPT/Gemini/Claude/Perplexity via logged-in browser; subs: console / deep-research / media | [[ava_builtins/skills/web_media/web-ai.ava.okf.md]] |
| web-sources | Fetch content from any source; per-platform adapter subs: generic / rss / youtube | [[ava_builtins/skills/web_media/web-sources.ava.okf.md]] |
| audio-transcribe | Transcribe audio/video / YouTube / URL to text (OpenAI, requires ffmpeg) | [[ava_builtins/skills/web_media/audio-transcribe.ava.okf.md]] |

## Key dependencies
- [[ava/skills.ava.okf.md|Skill System]] — skill mechanism and core-vs-instance origin axis
- [[web.ava.okf.md]] — web-sources fetching lands on `ava.web.search/fetch`
- [[ava/mcps.ava.okf.md]] — web-ai drives logged-in browser via chrome MCP ([[chrome.ava.okf.md]])
