"""Runs the editable-install guard against a venv and checkout; never imported by a production entry.

Run as ``python _guard_probe_child.py <venv-root> <allowed-checkout>``; exits non-zero on a violation.
"""

from __future__ import annotations

import sys
from pathlib import Path

from base.deploy.release.editable_install import editable_install_violations

raise SystemExit(
    bool(editable_install_violations(Path(sys.argv[1]), allowed_roots=(Path(sys.argv[2]),)))
)
