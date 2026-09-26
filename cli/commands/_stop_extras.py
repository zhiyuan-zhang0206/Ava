"""Full-stop of the macOS helper outside the application service roster.

Normal stop and destroy share exact-home helper retirement. Start recreates its
definition from the same signed artifact. Updates and pause preserve the helper.
"""

from __future__ import annotations


def stop_permissions_helper(*, force: bool = False, timeout_s: float = 30.0) -> None:
    """Stop this home's macOS helper; the Windows helper is user-wide."""
    from services.permissions_helper.launchd_job import unregister_helper
    from shared.config import settings
    from shared.paths import ava_home
    from shared.platform import IS_MACOS

    if IS_MACOS:
        unregister_helper(
            ava_home(),
            helper_port=settings.services.permissions_helper_port,
            force=force,
            timeout_s=timeout_s,
        )
