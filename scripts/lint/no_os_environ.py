"""Launcher of `meizel.state.no_os_environ`: runtime config goes through `Settings`.

Run: `.venv/bin/python scripts/lint/no_os_environ.py [path ...]`. The rule, its allowlist
and its rationale live in `meizel/state/no_os_environ.py`; this launcher only binds the
application's config registry (the env names `Settings` owns, which Rule 2 reads) and
enters a scratch home before that registry is imported.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
from base.host.env.dotenv_boot import enter_scratch_home  # noqa: E402

if __name__ == "__main__":
    enter_scratch_home()

from meizel.state import no_os_environ as rule  # noqa: E402 - standalone script


def settings_managed_aliases() -> frozenset[str]:
    """Every alias the config field registry owns — auto-syncs when Settings adds a field."""
    from base.config import FIELD_INFOS

    return frozenset(field.alias for field in FIELD_INFOS.values() if field.alias)


def main(argv: list[str] | None = None, *, repo_root: Path | None = None) -> int:
    return rule.main(argv, managed_aliases=settings_managed_aliases, repo_root=repo_root)


if __name__ == "__main__":
    sys.exit(main())
