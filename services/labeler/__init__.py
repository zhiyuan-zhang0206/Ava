"""Labeler daemon — standalone label auto-generation process.

Zero dependency on the Gateway. Provides:
- `daemon.py` — main loop, 1s polls agents table, auto-generates short
  names for new agents
- `healthcheck.py` — called by OS cron every minute, keepalive
- `labeler.py` — LLM label generation core logic (`generate_label_async`)
"""
