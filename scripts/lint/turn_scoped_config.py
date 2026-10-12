"""Launcher of `meizel.state.turn_scoped_config`: no per-agent config through a process global.

Run: `.venv/bin/python scripts/lint/turn_scoped_config.py [path ...]`. The rule and its
rationale live in `meizel/state/turn_scoped_config.py`; this launcher only binds the
application's config registry (which fields are per-agent) and enters a scratch home
before that registry is imported.
"""

from __future__ import annotations

import sys
from pathlib import Path

from base.host.env.dotenv_boot import enter_scratch_home

if __name__ == "__main__":
    enter_scratch_home()

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from meizel.state import turn_scoped_config as rule  # noqa: E402 - standalone script


def main(argv: list[str]) -> int:
    from base.config import per_agent_field_names

    return rule.main(argv, per_agent_field_names=per_agent_field_names)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
