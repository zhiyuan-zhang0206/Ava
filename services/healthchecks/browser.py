"""Identity probe for the browser owned by the root supervisor."""

from base.config import settings
from base.daemon.health import DaemonProbe
from services.browser.probe import probe_browser


def _probe() -> DaemonProbe:
    """Report browser protocol health and ownership without changing processes."""
    return probe_browser(settings.services.browser_cdp_port)
