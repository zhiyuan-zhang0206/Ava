"""Full-stop of the macOS helper outside the application service roster.

Normal stop and destroy share exact-home helper retirement. Start recreates its
definition, first rebuilding the signed artifact when its sources changed (only
a retired helper's artifact is replaced). Updates and restart preserve the helper.
"""

from __future__ import annotations


def stop_permissions_helper(*, force: bool = False, timeout_s: float = 30.0) -> None:
    """Stop this home's macOS helper; the Windows helper is user-wide."""
    from base.config import settings
    from base.native_process.os_platform import is_macos
    from base.paths import ava_home
    from services.desktop.permissions_helper.launchd_job import unregister_helper

    if is_macos():
        unregister_helper(
            ava_home(),
            helper_port=settings.services.permissions_helper_port,
            force=force,
            timeout_s=timeout_s,
        )
