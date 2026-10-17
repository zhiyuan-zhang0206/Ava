"""A remote unit's join: verify its gateway, install or require its database capability.

Settings-free, shared by `meiva init` (the unit's first join) and
`meiva cluster db-authority install-unit` (a fresher capability on an initialized
unit). `meiva start` does neither: a started unit fetches its configuration through
Settings and probes its gateway itself.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from base.host.net.predicates import is_loopback_host


def join_gateway(values: dict[str, str], home: Path, capability: str | None) -> None:
    """Verify a remote unit's gateway and install or require its capability.

    The unit never holds the human cluster secret: it authenticates with the
    machine API token of its capability — the carried bundle's at a join, the
    installed one's afterwards.
    """
    from base.host.db_authority.unit import install_bundle
    from base.host.env.bootstrap import fetch_bootstrap_config

    if "MEIVA_CLUSTER_SECRET" in values:
        raise ValueError(
            f"this remote unit's home ({home}) records the human cluster secret; a remote "
            "unit authenticates with its capability's machine API token and never holds "
            "it. Remove MEIVA_CLUSTER_SECRET from its .env"
        )
    gateway = values["MEIVA_GATEWAY_URL"]
    host = values.get("MEIVA_MACHINE_HOST", "")
    remote = not is_loopback_host(urlsplit(gateway).hostname or "")
    if remote and (not host or is_loopback_host(host)):
        raise ValueError("joining a remote gateway requires a reachable --machine-host")
    bundle, token = _join_credential(home, capability)
    payload = fetch_bootstrap_config(gateway, bearer=token)
    if remote and any(
        is_loopback_host(urlsplit(payload[key]).hostname or "")
        for key in ("MEIVA_DB_URL", "MEIVA_REDIS_URL")
    ):
        raise ValueError("remote gateway returned loopback data-plane URLs")
    if capability is not None and bundle is not None:
        installed = install_bundle(
            home,
            bundle,
            machine=values["MEIVA_MACHINE_NAME"],
            served_endpoint=payload["MEIVA_DB_URL"],
        )
        Path(capability).unlink()
        print(
            f"  ✓ database capability installed: write generation {installed.generation.number}; "
            f"bundle {capability} consumed"
        )
    # Verify connection facts without persisting a gateway-owned configuration cache.
    for key in payload:
        values.pop(key, None)


def _join_credential(home: Path, capability: str | None) -> tuple[Any, str]:
    """(the opened bundle or None, the API token the join presents).

    A carried bundle is opened (authenticated, unexpired) before anything is
    fetched; without one the installed capability must exist.
    """
    from base.host.db_authority.unit import (
        CAPABILITY_KEY_ENV,
        load_unit_capability,
        no_capability_message,
        open_bundle,
    )
    from base.host.verified_file import regular_bytes

    transport_key = os.environ.pop(CAPABILITY_KEY_ENV, "")
    bundle = None
    if capability is not None:
        if not transport_key:
            raise ValueError(
                f"a capability bundle requires its transport key in {CAPABILITY_KEY_ENV}"
            )
        bundle = open_bundle(regular_bytes(Path(capability), max_bytes=64 * 1024), transport_key)
    held = bundle.capability if bundle is not None else load_unit_capability(home)
    if held is None:
        raise ValueError(no_capability_message(home))
    return bundle, held.api.token
