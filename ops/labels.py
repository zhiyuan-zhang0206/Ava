"""Label publish helper — re-exports from shared.labels.

History: originally contained `generate_label_async` (LLM auto-generation)
+ `publish_label_updated` (Redis publish). `generate_label_async` has
moved to `services/labeler/labeler.py` (eliminates the services -> gateway
reverse dependency). `publish_label_updated` has moved to
`shared/labels.py` (shared by gateway + services).

This file is kept as a backward-compatible re-export — external callers'
import paths are unchanged.
"""

from shared.labels import publish_label_updated

__all__ = ["publish_label_updated"]
