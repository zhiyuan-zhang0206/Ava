"""Shared logic for the audio-transcribe skill's `transcribe.py` CLI.

`transcribe.py` holds the OpenAI speech-to-text transcription pipeline; the
`web-sources:youtube` skill's `reference/feed.py` imports it as the
whisper-fallback for videos with no captions.
"""
