"""Configuration slice of the memory-search daemon.

Fields keep their flat registry names. `services/memory_search/daemon.py` builds the slice
(`memory_search_config()`, the composition root) and reads from it; `app.py` takes what it
needs as arguments.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MemorySearchConfig:
    memory_search_pidfile: Path
    memory_search_data_dir: Path
    memory_search_port: int
    memory_search_max_batch_rows: int
