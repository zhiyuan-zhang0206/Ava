from __future__ import annotations

import sys


def declaration_projection() -> None:
    from ops.env_audit import load_alias_metadata

    metadata = load_alias_metadata()
    assert metadata["MEIVA_DB_URL"][1] is True
    assert "base.config._full" not in sys.modules
    assert "base.config.admin.metadata" not in sys.modules


if __name__ == "__main__":
    declaration_projection()
