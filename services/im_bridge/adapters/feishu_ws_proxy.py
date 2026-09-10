"""Env-proxy support for the lark-oapi WS handshake (issue #2089).

``lark_oapi`` pins ``proxy=None`` whenever the installed websockets exposes the
``proxy`` parameter (websockets>=15 — preserving its historical direct
connection), and ``lark.ws.Client`` takes no proxy parameter, so on a host
behind an HTTP proxy the long connection can never be established. The SDK's
module-level connect-kwargs builder is the only seam, and this module owns its
replacement so the adapter stays about bridging messages.

Wired in by ``FeishuAdapter._build_ws_client`` on the ws thread, before the
client is built: :func:`allow_env_proxy_for_ws`.
"""

from __future__ import annotations

import inspect
from typing import Any

from shared.env_registry import network_proxy_configured


def ws_connect_kwargs() -> dict[str, Any]:
    """``websockets.connect`` kwargs for the SDK's WS handshake — env-proxy aware.

    Mirrors the SDK's own version guard (the ``proxy`` parameter exists only on
    websockets>=15, exactly the branch ``lark_oapi/ws/client.py`` serves by
    pinning ``proxy=None``), but does not re-disable the environment discovery
    that pin turns off: with a proxy configured, omitting the argument leaves
    websockets' own default (``proxy=True`` — discover from ``HTTPS_PROXY`` /
    ``ALL_PROXY`` and honor ``NO_PROXY`` per target) in charge. Without one, the
    SDK's direct behavior stands unchanged.

    An ``ALL_PROXY``-only environment stays direct: websockets' discovery does
    not map the ``all`` scheme onto a websocket target, and the long-connection
    endpoint is wss — the standard ``HTTPS_PROXY`` spelling is what covers it.

    Which keys count as "configured" is the env registry's declaration
    (``shared/env_registry.py:NETWORK_PROXY_KEYS`` — the same keys a service
    child receives, so the WS path and the child env agree on one list).
    """
    import websockets

    if "proxy" not in inspect.signature(websockets.connect).parameters:
        return {}
    return {} if network_proxy_configured() else {"proxy": None}


def allow_env_proxy_for_ws() -> None:
    """Hand the SDK's kwargs builder over to :func:`ws_connect_kwargs`.

    ``Client._connect`` resolves the builder by name at connect time, so
    replacing the module attribute is enough; called on the ws thread before the
    client is built. The replacement is a superset of the SDK's behavior — a
    host with no proxy configured keeps connecting directly.
    """
    import lark_oapi.ws.client as ws_client  # pyright: ignore[reportUnknownVariableType]

    setattr(ws_client, "_ws_connect_kwargs", ws_connect_kwargs)  # noqa: B010 — a private SDK seam; the module attribute is untyped
