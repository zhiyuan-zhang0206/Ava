from __future__ import annotations

import sys

from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity

_ = MeivaContext(identity=AgentIdentity(1, True)).describe
loaded = sorted(m for m in ("psycopg_pool", "redis", "httpx", "langchain_core") if m in sys.modules)
sys.stdout.write(f"{loaded}\n")
