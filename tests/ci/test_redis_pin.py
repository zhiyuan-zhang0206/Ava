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


def test_ci_installs_the_canonical_redis_from_the_redis_io_repo() -> None:
    text = (_REPO_ROOT / ".github/actions/install-pg-redis/action.yml").read_text(encoding="utf-8")
    assert re.findall(r'^\s*redis_version="([^"]+)"$', text, re.MULTILINE) == [
        brew_pin.REDIS_APT_VERSION
    ]
    assert "https://packages.redis.io/deb" in text
    assert '"redis-server=${redis_version}" "redis-tools=${redis_version}"' in text
    major_minor = brew_pin.REDIS_APT_VERSION.removesuffix(".*")
    assert re.search(rf"^\s*{re.escape(major_minor)}\.\*\) ;;$", text, re.MULTILINE)
