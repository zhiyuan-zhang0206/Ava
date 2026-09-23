"""The visible half of the converge preserve contract.

A converge-managed destination whose current content no longer matches the
recorded render was hand-edited: the renderers warn and preserve it instead of
overwriting — see cli/commands/_rendered_file.py (LGTM provisioning +
otel-collector config), cli/commands/_converge_skills.py and
cli/commands/_converge_extensions.py. Before this
reporter those warnings lived only in converge output, and a frozen LGTM
dashboard survived three consecutive rollouts unnoticed (task #3689). Mirrors
shared/plugin_load_report.py: one `converge_file_preserved` telemetry event per
preserve hit, so the drift is visible on the observability surface without
reading the converge log. The event name is registered in
shared/events/registry_ops.py next to the dashboard render-failure guard.
"""

from __future__ import annotations

import contextlib


def report_converge_preserve(*, path: str, key: str, surface: str) -> None:
    """Report one preserved converge destination. Never raises.

    ``path`` is the destination that was preserved; ``key`` is the render key
    (hash-sidecar key, or the managed name for skills); ``surface`` names the
    renderer (``lgtm-dashboard``, ``lgtm-provisioning``, ``otel-collector``,
    ``skills``, ``extensions``).
    """
    from shared.telemetry import emit

    with contextlib.suppress(Exception):
        emit(
            "telemetry",
            "converge_file_preserved",
            level="warning",
            source="converge",
            attributes={"path": path, "key": key, "surface": surface},
        )
