"""Fresh-interpreter config probes for the config tests; never imported by a production entry.

Run as ``python _config_child.py <mode>`` with the environment prepared by the parent test. Most modes
print the resolved values, one per line (or one ``DEFER`` line); ``import`` only imports
``base.config``.
"""

from __future__ import annotations

import os
import sys

mode = sys.argv[1]
if mode == "import":
    import base.config  # noqa: F401  # pyright: ignore[reportUnusedImport]
elif mode == "data_plane_urls":
    import base.config as c

    sys.stdout.write(f"{c.settings.data_plane.db_url}\n{c.settings.data_plane.redis_url}\n")
elif mode == "db_url":
    import base.config as c

    sys.stdout.write(f"{c.settings.data_plane.db_url}\n")
elif mode == "db_url_events_channel":
    import base.config as c

    sys.stdout.write(f"{c.settings.data_plane.db_url}\n{c.settings.data_plane.events_channel}\n")
elif mode == "respawn":
    import base.config as c

    sys.stdout.write(
        f"{c.settings.data_plane.db_url}\n"
        f"{os.environ['DEEPSEEK_API_KEY']}\n"
        f"{os.environ['AVA_HOME']}\n"
    )
elif mode == "skip_defers_prepare":
    import base.config as c
    from base.host.env.dotenv_boot import PLACEHOLDER_DB_URL

    prepared_before = c.boot_state()["prepared"]
    value = c.settings.lm.llm_model
    sys.stdout.write(
        f"DEFER {prepared_before} {c.boot_state()['prepared']} {value} "
        f"{os.environ.get('AVA_DB_URL') == PLACEHOLDER_DB_URL}\n"
    )
elif mode == "eval_isolation":
    from base.config.domains.agent.eval import AgentEvalSettings

    config = AgentEvalSettings()
    assert config.eval_isolation is True
    assert config.eval_network_allowlist == ["web", "understand"]
    sys.stdout.write("ok\n")
elif mode == "ava_home":
    import base.paths as p

    sys.stdout.write(f"{p.ava_home()}\n")
else:
    raise SystemExit(f"unknown mode: {mode}")
