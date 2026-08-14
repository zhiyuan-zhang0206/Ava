"""Restart dispatcher daemon — standalone restart dispatch process.

Zero dependency on the Gateway (only imports
gateway.agents.respawn_agent). Provides:
- `daemon.py` — main loop, 1s polls agents.status='restarting',
  second-level respawn
- `healthcheck.py` — called by OS cron every minute, keepalive +
  re-delivery

Agent SDK (`ava.self.restart()`) is unchanged.
"""
