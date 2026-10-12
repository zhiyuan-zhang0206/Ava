"""Fresh-interpreter run of the real `ava init` then `ava start` dispatch into a new home.

Identity publication, then the sinks, then a service phase replaced by the real pgvector
pre-create against a port nothing listens on. The parent test reads this process's stderr
and the log it wrote.
"""

import os
from pathlib import Path
from typing import Any

from base import cluster
from base.cluster import ensure_pgvector_extension
from base.log import prepare_process_logging
from cli import main, start_intent


def start(**_kwargs: Any) -> int:
    ensure_pgvector_extension(
        "ava", base_admin_url="postgresql://probe@127.0.0.1:1/postgres?connect_timeout=5"
    )
    return 0


prepare_process_logging()
home = Path(os.environ["AVA_HOME"])
checkout = home.parent / "checkout"
checkout.mkdir()
start_intent._checkout = lambda: checkout
cluster.port_free = lambda _port: True
start_intent.run_start_command = start
start_intent.complete_start = lambda: None
assert (
    main.main(["init", "--serve-gateway", "--serve-agent-runner", "--machine-name", "probe"]) == 0
)
raise SystemExit(main.main(["start"]))
