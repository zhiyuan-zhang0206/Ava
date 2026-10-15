from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MemorySearchConfig:
    memory_search_pidfile: Path
    memory_search_data_dir: Path
    memory_search_port: int
    memory_search_max_batch_rows: int
