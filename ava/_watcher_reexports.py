"""Back-compat aliases for the watcher reconcile surface.

Split out of ``watcher.py`` at the 800-line ceiling (issue #2078):
``ava.watcher`` imports these names at the bottom of its module, so
historical imports keep resolving on the same module object. The
``__module__`` fixup preserves the pre-split defining module for
introspection and pickling.
"""

from __future__ import annotations

import ava._watcher_reconcile as _reconcile

_kill_watcher_orphan_processes = _reconcile._kill_watcher_orphan_processes
_live_cron_session = _reconcile._live_cron_session
_notify_missed_watcher = _reconcile._notify_missed_watcher
_reconcile_missing = _reconcile._reconcile_missing
_reap_superseded_watcher = _reconcile._reap_superseded_watcher
_rebuild_stale_cron_watcher = _reconcile._rebuild_stale_cron_watcher
reconcile = _reconcile.reconcile

# Preserve the historical defining module for introspection and pickling.
for _moved_function in (
    _kill_watcher_orphan_processes,
    _live_cron_session,
    _notify_missed_watcher,
    _reconcile_missing,
    _reap_superseded_watcher,
    _rebuild_stale_cron_watcher,
    reconcile,
):
    _moved_function.__module__ = "ava.watcher"
del _moved_function
