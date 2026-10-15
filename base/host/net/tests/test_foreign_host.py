from __future__ import annotations

import pytest

from base.host.net.url_secret import names_foreign_host


@pytest.mark.parametrize(
    ("urls", "foreign"),
    [
        (("postgresql://ava@127.0.0.1:5432/ava", "redis://127.0.0.1:6380/0"), False),
        (("postgresql://ava@localhost/ava", "redis://[::1]:6380/0"), False),
        (("postgresql://ava@10.0.0.7:5432/ava", "redis://127.0.0.1:6380/0"), True),
        (("postgresql://ava@127.0.0.1:5432/ava", "rediss://ava@cache.example:6380/0"), True),
        (("postgresql:///ava", "redis:///0"), False),
        ((), False),
    ],
)
def test_any_non_loopback_url_makes_the_plane_foreign(urls: tuple[str, ...], foreign: bool) -> None:
    assert names_foreign_host(*urls) is foreign
