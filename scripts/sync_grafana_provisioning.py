#!/usr/bin/env python3
"""Standalone entry for the Grafana provisioning sync (Task #1224 follow-up).

`cli/commands/_update_local.py:_checkout_and_sync` invokes this script
through the NEW tree's venv after checkout, so the copy blocks shipped by
the very rollout being deployed take effect on that same rollout — the
updater process itself executes the pre-checkout module, and calling
`_sync_grafana_provisioning` in-process made every newly-added copy block
land one rollout late (prod 2026-08-13: #2509's contact.yml and #2515's
datasources.yml never copied while the older rules.yml block did).

Usage: python scripts/sync_grafana_provisioning.py <repo-root>

The repo is uv-installed into the venv (editable), so `cli` imports resolve
from any cwd; `AVA_HOME` comes from the checkout-anchored boot as usual.
"""

from __future__ import annotations

import sys
from pathlib import Path

from cli.commands._update_local import _sync_grafana_provisioning


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: sync_grafana_provisioning.py <repo-root>", file=sys.stderr)
        return 2
    _sync_grafana_provisioning(Path(sys.argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
