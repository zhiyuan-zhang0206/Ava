"""Identity probe for the browser owned by the root supervisor."""

from services.browser.probe import probe_browser
from shared.config import settings
from shared.daemon_health import DaemonProbe


def _probe() -> DaemonProbe:
    """Report browser protocol health and ownership without changing processes."""
    return probe_browser(settings.services.browser_cdp_port)
