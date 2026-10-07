#!/usr/bin/env python3
"""Stable CLI path and reusable GitHub CI status imports.

Implementation lives in scripts.ci: status owns GitHub evidence, monitor owns
read-only polling, commands routes the CLI, and owner_operations handles explicit
queue or re-run commands. Missing or queued execution cannot be forced green.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.ci.pull_requests.commands import main as main
from scripts.ci.pull_requests.status import CIResult as CIResult
from scripts.ci.pull_requests.status import CIStatus as CIStatus
from scripts.ci.pull_requests.status import check_ci as check_ci

if __name__ == "__main__":
    sys.exit(main())
