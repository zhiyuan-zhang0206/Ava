"""Applies an explicit process timezone in a fresh interpreter; never imported by a production entry.

Run as ``python _process_timezone_child.py <json-encoded name>``.
"""

from __future__ import annotations

import json
import os
import sys

from base.host.env.dotenv_boot import apply_process_timezone

assert "base.config" not in sys.modules
name = json.loads(sys.argv[1])
apply_process_timezone(name)
expected = "Asia/Shanghai" if name == "Asia/Shanghai" else "UTC"
assert os.environ["TZ"] == expected
assert "base.config" not in sys.modules
