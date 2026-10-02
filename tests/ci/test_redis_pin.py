"""The Redis series every Linux install path uses comes from one canonical source.

``base.host.brew_pin.REDIS_APT_VERSION`` is canonical: the approved Redis 8.2
series, as macOS pins ``redis@8.2``. The provision script (bash, runs before Python
exists) embeds the same apt version string; these tests fail when the copy drifts
or stops installing and holding the server and tools at it.
"""

from __future__ import annotations

import re
from pathlib import Path

from base.host import brew_pin

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_the_canonical_pin_is_the_approved_8_2_series() -> None:
    assert re.fullmatch(r"6:8\.2\.\*", brew_pin.REDIS_APT_VERSION)
    assert "redis@8.2" in brew_pin.PINNED_BREW_FORMULAE


def test_provision_script_installs_and_holds_the_canonical_redis() -> None:
    text = (_REPO_ROOT / "scripts/provision/database.sh").read_text(encoding="utf-8")
    assert re.findall(r'^REDIS_APT_VERSION="([^"]+)"$', text, re.MULTILINE) == [
        brew_pin.REDIS_APT_VERSION
    ]
    assert (
        'prov_apt_install "redis-server=${REDIS_APT_VERSION}" "redis-tools=${REDIS_APT_VERSION}"'
        in text
    )
    assert "apt-mark hold redis-server redis-tools" in text
