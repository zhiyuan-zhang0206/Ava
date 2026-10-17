import os
from pathlib import Path
from typing import Any

from base import cluster
from base.cluster import ensure_pgvector_extension
from base.log import prepare_process_logging
from cli import main, start_intent


def start(**_kwargs: Any) -> int:
    ensure_pgvector_extension(
        "meiva", base_admin_url="postgresql://probe@127.0.0.1:1/postgres?connect_timeout=5"
    )
    return 0


prepare_process_logging()
home = Path(os.environ["MEIVA_HOME"])
checkout = home.parent / "checkout"
checkout.mkdir()
start_intent.checkout = lambda: checkout
cluster.port_free = lambda _port: True
start_intent.run_start_command = start
start_intent.complete_start = lambda **_kw: None
assert (
    main.main(["init", "--serve-gateway", "--serve-agent-runner", "--machine-name", "probe"]) == 0
)
raise SystemExit(main.main(["start"]))
