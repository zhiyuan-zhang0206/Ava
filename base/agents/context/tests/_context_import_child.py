"""Builds an AvaContext in a fresh interpreter; never imported by a production entry.

Run as ``python -I _context_import_child.py``; prints the connection-stack modules it loaded.
"""

from __future__ import annotations

import sys

from base.agents.context import AvaContext
from base.agents.context.identity import AgentIdentity

_ = AvaContext(identity=AgentIdentity(1, True)).describe
loaded = sorted(m for m in ("psycopg_pool", "redis", "httpx", "langchain_core") if m in sys.modules)
sys.stdout.write(f"{loaded}\n")
