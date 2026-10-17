---
type: doc
title: meiva.understand — Multimodal Understanding Primitive
description: '`meiva.understand` is the SDK''s single multimodal LLM primitive: asks the model to answer a prompt based on a file or a piece of text, supporting text/image/video/audio/PDF.'
tags:
- agent-view
- sdk
- agent-lifecycle
---

# meiva.understand — Multimodal Understanding Primitive

## What it is

`meiva.understand` is the core SDK's single multimodal LLM primitive—asks the model to answer a `prompt` based on given material. `meiva.web.fetch` also uses it internally to 'read' web pages.

## Core API

- `understand(targets: list[dict], max_concurrent=12) → list[str]` — Batch-only. Each target is a dict with `prompt` plus exactly one of `text` / `paths`. Answers come back in input order.
  - `paths`: non-empty list of file paths (relative path resolution same as other file operations) — a single file is a one-element list. Text files are read as UTF-8; images/video/audio/PDF are processed via media path based on detected MIME (inline media parts on the media model; size limits apply). Every file is sent in ONE model call as separate parts, so the model can compare them (e.g. a design frame plus a page screenshot); any media file routes the whole call to the media model.
  - `text`: material itself (literal string).

There is no single-call form — one question is a one-element list:

```python
[answer] = meiva.understand([{"prompt": "summarize this", "paths": ["notes.md"]}])
```

Targets run with a default ceiling of 12; pass a positive `max_concurrent=N` to choose another in-flight limit. The shared DeepSeek cap queues excess provider calls, and terminal typed provider failures surface as `UnderstandError`. Same batch shape as [[meiva/web/docs/web.meiva.okf.md]]'s `search` / `fetch`.

## Constraints & Errors
- `TypeError` — `targets` is not a list, or an element is not a dict.
- `ValueError` — a target is missing `prompt`, does not carry exactly one of `text` / `paths`, or `paths` is an empty list.
- `TypeError` — `targets[i]['paths']` is not a list, or an element is not a path string.
- `FileNotFoundError` — a target's `paths` entry does not point to an existing file.
- `UnderstandError` — a typed provider call failed permanently or exhausted its retries, or a file in `paths` is binary, cannot be read as UTF-8, and its extension is not in supported media suffixes.

Validation runs over every target before any model call, so a malformed batch fails before spending tokens. The first error from a running batch propagates.
Unknown model-invocation errors retain their original exception and traceback
after one attempt; error wording alone never grants provider retry authority.

## Key Dependencies
- [[meiva/docs/files.meiva.okf.md]] — `paths` uses the same path resolution (workspace-relative)
- [[base/lm/docs/lm/lm.meiva.okf.md]] — underlying chat model factory (text/media models come from settings)

## Notes
Text and media models come from `settings.lm.understand_text_model` (default `deepseek-flash`) / `settings.lm.understand_media_model` (default Gemini 3.8 Flash) respectively. The media model's provider is picked by its prefix through the same model factory every LLM path uses (`base/lm/factory.py`), and `MEIVA_UNDERSTAND_MEDIA_BASE_URL` can point the Gemini endpoint at a self-hosted relay. The media wire format is Gemini-specific, so the path currently supports Gemini models only — a non-Gemini media model fails fast with a clear error; the abstraction (factory routing / endpoint override / quality knobs) is in place for a second media provider to plug in with its own part conversion. Media goes inline; exceeding inline limits will raise `UnderstandError`.

## Structure
`meiva/understand/__init__.py` is the namespace function: it validates through `plans.normalize` (no ambient reads), builds this process's `UnderstandDeps` in `wiring.py` (the only place that reads the configured models and knobs, the model catalog, the provider factory, the bound agent's workspace and the clock) and calls `answer.run`. `answer.py` holds the provider calls, the media rules and the saved output; everything it touches arrives in the deps, so tests hand it a fake factory and the values they want instead of patching settings.
