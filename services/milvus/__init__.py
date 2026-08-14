"""Milvus-lite gRPC server (standalone), decoupled from memory_indexer /
agent search.

milvus-lite's in-process mode uses `fcntl.flock` for exclusive access to
the db file — multi-process access is not allowed (memory_indexer
daemon and agent search are different processes and would collide).
Standalone server mode has milvus-lite start its own gRPC server; multi
clients connect through the same URI.

prod / dev / eval / CI environments are consistent; all connect via the
server URI (`http://127.0.0.1:19530`) — no in-process /
lite-in-process mixed test-prod drift.

Layout:
- `daemon.py` — exec `milvus-lite server` (thin wrapper, lets the ava
  CLI use the standard `python -m services.milvus.daemon` start pattern)
- `healthcheck.py` — TCP probe + spawn restart (same pattern as other services)
"""
