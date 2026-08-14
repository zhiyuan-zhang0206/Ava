"""`python -m cli` entry point — forwards to `cli.main:main`.

Normally invoked via the `ava` script (entry point `cli.main:main`); this module
is the extra `python -m cli` entry, just a one-line forward.
"""

from __future__ import annotations

import sys

from cli.main import main

if __name__ == "__main__":
    sys.exit(main())
