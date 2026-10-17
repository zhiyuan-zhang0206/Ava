---
type: doc
title: web-sources skill — Fetch content from any source
description: "Fetch content from any internet source — follow updates from a creator/site/feed, or fetch a single URL — via per-platform adapter sub-skills. Pick the adapter first, then read its own skill; subs: generic / rss / youtube."
tags:
- extensions
- agent-instruction
---

# web-sources skill — Fetch content from any source

## What it is
A set of skills (`$MEIVA_HOME/skills/web-sources/`) to fetch content from any internet source: follow updates from a creator / site / feed, or fetch a single URL. Its organizational choice is **per-platform adapters** — each platform gets a self-contained sub-skill (pick the adapter first, then read its own SKILL.md), instead of one giant generic scraper. The root is a routing index.

## Four adapter sub-skills
- **youtube** — enumerate new videos from channel/playlist + fetch metadata and transcription; no login required, runs on any machine.
- **rss** — parse RSS/Atom feeds: list items / ingest a window, optionally fetch full text of each item.
- **generic** — extract body text of any webpage (remove boilerplate → markdown + metadata), or enumerate article links from index pages by URL regex; it's the fallback for sources that have **no platform adapter and no RSS** (official sites / forums / newsletters). `enum` runs the same curl → Jina escalation ladder as `fetch`.

## Key dependencies
- [[meiva_builtins/skills/docs/skills.meiva.okf.md|Skills index]] — full skills catalog
- [[meiva/web/docs/web.meiva.okf.md|meiva.web]] — fetching lands on `meiva.web.search/fetch`
- [[meiva_builtins/skills/integrations/audio-transcribe/docs/audio-transcribe.meiva.okf.md|audio-transcribe]] — transcription of audio/video items without subtitles relies on it
