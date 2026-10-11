"""`python -m cli` entry point — forwards to `cli.main:entry`.

Normally invoked via the `ava` script (entry point `cli.main:entry`); this module
is the extra `python -m cli` entry, just a one-line forward.
"""

from __future__ import annotations

import sys

from cli.main import entry

if __name__ == "__main__":
    sys.exit(entry())
