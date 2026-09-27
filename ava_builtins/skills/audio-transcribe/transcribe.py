#!/usr/bin/env python3
"""Thin CLI entry point for OpenAI speech-to-text transcription.

The transcription logic lives in
`ava_builtins.skill_support.audio_transcribe.transcribe` (also imported by
the `web-sources:youtube` skill's `reference/feed.py` as the whisper
fallback for captionless videos); see that module for the full docs.

Usage:
    .venv/bin/python skills/audio-transcribe/transcribe.py <source> ...
"""

from __future__ import annotations

from ava_builtins.skill_support.audio_transcribe.transcribe import main

if __name__ == "__main__":
    main()
