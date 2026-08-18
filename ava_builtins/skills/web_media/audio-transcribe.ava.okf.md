---
type: doc
title: audio-transcribe skill — Audio/video to text
description: Convert local audio/video files, YouTube videos, or media URLs to plain text via OpenAI. Requires ffmpeg + OPENAI_API_KEY. Used when transcription of audio/video/podcast is needed, or when a feed item lacks subtitles/transcription for supplementation.
tags:
- extensions
- agent-instruction
---

# audio-transcribe skill — Audio/video to text

## What it is
A skill (`$AVA_HOME/skills/audio-transcribe/`) that converts local audio/video files, YouTube videos, or media URLs to plain text via OpenAI transcription. It exists as a **supplementation layer** for multimodal sources: when a feed item **lacks** existing subtitles/transcription, the web-sources side falls through here to transcribe the audio track into text, allowing downstream to process it like text.

## Dependency form
Requires `ffmpeg` (extract / split audio track) + `OPENAI_API_KEY`. It's a metered API path (contrast with web-ai's "use logged-in browser to avoid credit" — transcription has no corresponding free web surface, so it goes through API).

## Key dependencies
- [[ava_builtins/skills/web_media/web_media.ava.okf.md|Web & multimodal skill]] — belongs to functional group
- [[ava_builtins/skills/web_media/web-sources.ava.okf.md|web-sources]] — upstream: items without subtitles are sent here for transcription
