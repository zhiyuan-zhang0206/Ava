from __future__ import annotations

import sys
import time
from pathlib import Path

from base.cluster.dataplane import pg_tools
from base.cluster.dataplane.pg_throwaway_base import ThrowawayConfig

cm = pg_tools.throwaway_postgres(config=ThrowawayConfig(Path(sys.argv[1])))
url = cm.__enter__()
target = Path(sys.argv[2])
part = Path(sys.argv[2] + ".part")
part.write_text(url)
part.replace(target)
time.sleep(600)
