"""Fresh-interpreter env/config import probes; never imported by a production entry.

Run as ``python [-I] _env_import_child.py <scenario>`` with the environment prepared by the test.
"""

from __future__ import annotations

import sys


def declaration_projection() -> None:
    from ops.env_audit import _load_alias_metadata

    metadata = _load_alias_metadata()
    assert metadata["AVA_DB_URL"][1] is True
    assert "base.config._full" not in sys.modules
    assert "base.config.admin.metadata" not in sys.modules


def forwarded_timezone() -> None:
    from base.config import settings

    sys.stdout.write(f"{settings.general.timezone}\n")


def registry_imports_first() -> None:
    import base.host.env.registry  # pyright: ignore[reportUnusedImport]

    # isort: split
    import base.config  # noqa: F401  # pyright: ignore[reportUnusedImport]

    sys.stdout.write("ok\n")


SCENARIOS = {
    "declaration_projection": declaration_projection,
    "forwarded_timezone": forwarded_timezone,
    "registry_imports_first": registry_imports_first,
}

if __name__ == "__main__":
    SCENARIOS[sys.argv[1]]()
