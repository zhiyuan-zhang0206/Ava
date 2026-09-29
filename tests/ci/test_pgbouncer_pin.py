"""The PgBouncer version every Linux install path uses comes from one canonical source.

``base.host.brew_pin.PGBOUNCER_APT_VERSION`` is canonical. The provision script
(bash, runs before Python exists) and the CI install action embed the same pgdg
version string; these tests fail when either copy drifts.
"""

from __future__ import annotations

import re
from pathlib import Path

from base.host import brew_pin

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_provision_script_installs_the_canonical_pgbouncer() -> None:
    text = (_REPO_ROOT / "scripts/provision/database.sh").read_text(encoding="utf-8")
    assert re.findall(r'^PGBOUNCER_APT_VERSION="([^"]+)"$', text, re.MULTILINE) == [
        brew_pin.PGBOUNCER_APT_VERSION
    ]
    assert 'prov_apt_install "pgbouncer=${PGBOUNCER_APT_VERSION}"' in text
    assert "apt-mark hold pgbouncer" in text


def test_ci_installs_the_canonical_pgbouncer() -> None:
    text = (_REPO_ROOT / ".github/actions/install-pg-redis/action.yml").read_text(encoding="utf-8")
    assert re.findall(r'^\s*pgbouncer_version="([^"]+)"$', text, re.MULTILINE) == [
        brew_pin.PGBOUNCER_APT_VERSION
    ]
