---
type: doc
title: ava.understand — Multimodal Understanding Primitive
description: '`ava.understand` is the SDK''s single multimodal LLM primitive: asks the model to answer a prompt based on a file or a piece of text, supporting text/image/video/audio/PDF.'
tags:
- agent-view
- sdk
- agent-lifecycle
---

# ava.understand — Multimodal Understanding Primitive

## What it is

`ava.understand` is the core SDK's single multimodal LLM primitive—asks the model to answer a `prompt` based on given material. `ava.web.fetch` also uses it internally to 'read' web pages.

## Core API

- `understand(targets: list[dict], max_concurrent=None) → list[str]` — Batch-only. Each target is a dict with `prompt` plus exactly one of `text` / `paths`. Answers come back in input order.
  - `paths`: non-empty list of file paths (relative path resolution same as other file operations) — a single file is a one-element list. Text files are read as UTF-8; images/video/audio/PDF are processed via media path based on detected MIME (inline media parts on the media model; size limits apply). Every file is sent in ONE model call as separate parts, so the model can compare them (e.g. a design frame plus a page screenshot); any media file routes the whole call to the media model.
  - `text`: material itself (literal string).

There is no single-call form — one question is a one-element list:

```python
[answer] = ava.understand([{"prompt": "summarize this", "paths": ["notes.md"]}])
```

Targets run concurrently by default with no ceiling; pass `max_concurrent=N` to cap how many targets are in flight at once (useful under a provider rate limit). Rate limiting beyond that belongs to the provider: a 429 surfaces as `UnderstandError` rather than being retried or backed off. Same batch shape as [[web.ava.okf.md]]'s `search` / `fetch`.

## Constraints & Errors
- `TypeError` — `targets` is not a list, or an element is not a dict.
- `ValueError` — a target is missing `prompt`, does not carry exactly one of `text` / `paths`, or `paths` is an empty list.
- `TypeError` — `targets[i]['paths']` is not a list, or an element is not a path string.
- `FileNotFoundError` — a target's `paths` entry does not point to an existing file.
- `UnderstandError` — model call failed, or a file in `paths` is binary, cannot be read as UTF-8, and its extension is not in supported media suffixes.

Validation runs over every target before any model call, so a malformed batch fails before spending tokens. The first error from a running batch propagates.

## Key Dependencies
- [[files.ava.okf.md]] — `paths` uses the same path resolution (workspace-relative)
- [[shared/lm/lm.ava.okf.md]] — underlying chat model factory (text/media models come from settings)

## Notes
Text and media models come from `settings.lm.understand_text_model` (default DeepSeek V4 Flash) / `settings.lm.understand_media_model` (default Gemini 3.5 Flash) respectively. The media model's provider is picked by its prefix through the same model factory every LLM path uses (`shared/lm/factory.py`), so a non-Gemini multimodal model id works too and `AVA_UNDERSTAND_MEDIA_BASE_URL` can point the Gemini endpoint at a self-hosted relay. The Gemini-only knobs (`AVA_UNDERSTAND_MEDIA_RESOLUTION` / `AVA_UNDERSTAND_MEDIA_THINKING_LEVEL`) apply to Gemini models; other providers ignore them. Media goes inline; exceeding inline limits will raise `UnderstandError`.
