"""A remote unit's join: verify its gateway, install or require its database capability.

Settings-free, shared by `ava init` (the unit's first join) and
`ava cluster db-authority install-unit` (a fresher capability on an initialized
unit). `ava start` does neither: a started unit fetches its configuration through
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
    installed one's afterwards (none when the cluster's API is open).
    """
    from base.cluster.authority.unit import install_bundle
    from base.host.env.bootstrap import fetch_bootstrap_config

    if "AVA_CLUSTER_SECRET" in values:
        raise ValueError(
            f"this remote unit's home ({home}) records the human cluster secret; a remote "
            "unit authenticates with its capability's machine API token and never holds "
            "it. Remove AVA_CLUSTER_SECRET from its .env"
        )
    gateway = values["AVA_GATEWAY_URL"]
    host = values.get("AVA_MACHINE_HOST", "")
    remote = not is_loopback_host(urlsplit(gateway).hostname or "")
    if remote and (not host or is_loopback_host(host)):
        raise ValueError("joining a remote gateway requires a reachable --machine-host")
    bundle, token = _join_credential(home, capability, remote=remote)
    payload = fetch_bootstrap_config(gateway, bearer=token)
    if remote and any(
        is_loopback_host(urlsplit(payload[key]).hostname or "")
        for key in ("AVA_DB_URL", "AVA_REDIS_URL")
    ):
        raise ValueError("remote gateway returned loopback data-plane URLs")
    if capability is not None and bundle is not None:
        installed = install_bundle(
            home, bundle, machine=values["AVA_MACHINE_NAME"], served_endpoint=payload["AVA_DB_URL"]
        )
        Path(capability).unlink()
        print(
            f"  ✓ database capability installed: write generation {installed.generation.number}; "
            f"bundle {capability} consumed"
        )
    # Verify connection facts without persisting a gateway-owned configuration cache.
    for key in payload:
        values.pop(key, None)


def _join_credential(home: Path, capability: str | None, *, remote: bool) -> tuple[Any, str]:
    """(the opened bundle or None, the API token the join presents; "" when open).

    A carried bundle is opened (authenticated, unexpired) before anything is
    fetched; without one the installed capability must exist.
    """
    from base.cluster.authority.unit import (
        CAPABILITY_KEY_ENV,
        load_unit_capability,
        no_capability_message,
        open_bundle,
    )
    from base.deploy.release.verified_file import regular_bytes

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
    if remote and held.api is None:
        raise ValueError(
            "the capability carries no API token (it was issued by a gateway with an open "
            "API), but a remote gateway authenticates; issue a new bundle on the gateway"
        )
    return bundle, "" if held.api is None else held.api.token
