"""Mints a maintenance hold driver and prints it; never imported by a production entry.

Run as ``python _hold_mint_child.py``; stdout is the encoded driver as one JSON line.
"""

from __future__ import annotations

import json
import sys

from base.deploy.maintenance.hold_driver import mint_driver

sys.stdout.write(json.dumps(mint_driver().encode(), sort_keys=True) + "\n")
sys.stdout.flush()
