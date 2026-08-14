"""`_live_pg_socket_dir` — the rolling-cutover socket probe.

`_start_pg` skips a pg that is already running, so across the path-only cutover
a pre-cutover instance keeps listening on the old name-keyed
`/tmp/ava-pg-<cluster>` dir while the canonical dir is the new slug one. Every
backend consumer of the socket (the provisioning admin dial via `pg_admin_url`,
AND pgbouncer's rendered `host=` — cli/commands/_pgbouncer.py) must therefore
dial the dir the running pg actually serves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import cli.commands._cluster_instance as ci


@pytest.fixture()
def canonical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "canonical"
    d.mkdir()
    monkeypatch.setattr(ci, "_pg_socket_dir", lambda: d)
    return d


def _sock(d: Path, port: int) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / f".s.PGSQL.{port}").write_text("")


def test_canonical_socket_wins(canonical: Path, tmp_path: Path) -> None:
    """A socket in the canonical dir is preferred even when a legacy dir also
    matches (post-restart state: pg moved to the new dir)."""
    _sock(canonical, 5433)
    probe_root = tmp_path / "probe"
    _sock(probe_root / "ava-pg-main", 5433)
    assert ci._live_pg_socket_dir(5433, probe_root=probe_root) == canonical


def test_legacy_dir_found_by_port(canonical: Path, tmp_path: Path) -> None:
    """No canonical socket + a legacy name-keyed dir serving OUR port → that dir
    (matched by the port, which is data — never by any name)."""
    probe_root = tmp_path / "probe"
    legacy = probe_root / "ava-pg-main"
    _sock(legacy, 5433)
    _sock(probe_root / "ava-pg-other", 18011)  # another cluster's port — not ours
    assert ci._live_pg_socket_dir(5433, probe_root=probe_root) == legacy


def test_fallback_to_canonical_when_nothing_listens(canonical: Path, tmp_path: Path) -> None:
    """Fresh birth: nothing listens yet — the canonical dir is returned (pg is
    about to be started there)."""
    probe_root = tmp_path / "probe"
    probe_root.mkdir()
    assert ci._live_pg_socket_dir(5433, probe_root=probe_root) == canonical


def test_pgbouncer_ini_renders_the_live_socket_dir(
    canonical: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pooler's backend `host=` must go through the same live-socket probe as
    the admin dial — rendering the canonical dir against a pre-cutover pg breaks
    every pooled query while admin readiness stays green."""
    import cli.commands._pgbouncer as pgb

    legacy = tmp_path / "probe" / "ava-pg-main"
    _sock(legacy, 5433)
    monkeypatch.setattr(
        pgb,
        "_live_pg_socket_dir",
        lambda port: ci._live_pg_socket_dir(port, tmp_path / "probe"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(ci, "_bind_addrs", lambda _secret: ["127.0.0.1"])  # pyright: ignore[reportUnknownArgumentType]
    ini = pgb._render_ini(
        pg_port=5433,
        listen_port=6433,
        db_name="ava_main",
        role="ava_main",
        cluster_secret="s3cr3t",  # noqa: S106 — test fixture
    )
    assert f"host={legacy} port=5433" in ini
