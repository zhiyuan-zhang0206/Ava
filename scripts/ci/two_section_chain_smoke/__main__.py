"""`python -m scripts.ci.two_section_chain_smoke` — and `python scripts/ci/two_section_chain_smoke/`.

The path form needs the checkout on sys.path: PYTHONSAFEPATH keeps a script's
own directory off it, and the module form resolves `scripts.ci...` from the
current directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.ci.two_section_chain_smoke.cli import main  # noqa: E402 - checkout path guard above

if __name__ == "__main__":
    raise SystemExit(main())
