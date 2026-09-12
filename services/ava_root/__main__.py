"""`python -m services.ava_root` — run the daemon."""

from __future__ import annotations

import sys

from services.ava_root.daemon import main

if __name__ == "__main__":
    sys.exit(main())
