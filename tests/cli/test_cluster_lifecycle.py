"""Unit tests for the path-only cluster lifecycle helpers
(cli/commands/cluster_lifecycle.py): registry allocation, the `ava start`
installed-home gate, and `ava cluster ls/down/destroy` addressed by home path.

All side-effecting steps are monkeypatched so no real pg/redis/subprocess is
needed. Install-time birth itself is covered by tests/cli/test_install_cluster.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from shared import cluster as cl
from shared.cluster import LEGACY_AVA_PORTS, ClusterPorts, ClusterRecord, get_record
from shared.port_block import PORT_OFFSETS

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point registry_path() at a tmp file so tests don't touch the real registry."""
    reg_file = tmp_path / "clusters.json"
    monkeypatch.setattr(cl, "registry_path", lambda: reg_file)
    return reg_file


def _full_ports(base: int) -> ClusterPorts:
    return cast("ClusterPorts", {svc: base + off for svc, off in PORT_OFFSETS.items()})


# ---------------------------------------------------------------------------
# _ensure_record — path-keyed allocation
# ---------------------------------------------------------------------------


def test_ensure_record_default_home_uses_legacy_ports(
    isolated_registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cli.commands.cluster_lifecycle as gw

    monkeypatch.setattr(cl, "_port_free", lambda _port: True)  # pyright: ignore[reportUnknownArgumentType]
    rec, created = gw._ensure_record(cl.default_home())
    assert created is True
    assert rec.gateway_home == str(cl.default_home())
    assert rec.ports["gateway"] == LEGACY_AVA_PORTS["gateway"]  # 8000
    assert rec.ports["postgres"] == 5433  # its own fixed instance ports
    assert rec.ports["redis"] == 6380


def test_ensure_record_allocates_block_for_dev_home(
    isolated_registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cli.commands.cluster_lifecycle as gw

    monkeypatch.setattr(cl, "_port_free", lambda _port: True)  # pyright: ignore[reportUnknownArgumentType]
    home = tmp_path / ".ava-dev"
    rec, created = gw._ensure_record(home)
    assert created is True
    assert rec.gateway_home == str(home)
    # a dev cluster gets its own pg/redis ports inside its allocated block
    assert rec.ports["postgres"] == rec.ports["gateway"] + 11
    assert rec.ports["redis"] == rec.ports["gateway"] + 12
    # second call: reuse, not re-allocate
    rec2, created2 = gw._ensure_record(home)
    assert created2 is False
    assert rec2 == rec


def test_concurrent_ensure_record_no_collision(
    isolated_registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent _ensure_record (the registry read-allocate-save critical section)
    must serialize under registry_lock so no two clusters get the same port block
    (and with it, the same pg/redis instance ports)."""
    import threading

    import cli.commands.cluster_lifecycle as gw

    monkeypatch.setattr(cl, "_port_free", lambda _port: True)  # pyright: ignore[reportUnknownArgumentType]
    homes = [tmp_path / f".ava-t{i}" for i in range(6)]
    results: dict[str, object] = {}

    def make(h: Path) -> None:
        rec, _created = gw._ensure_record(h)
        results[str(h)] = rec

    threads = [threading.Thread(target=make, args=(h,)) for h in homes]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    recs = [results[str(h)] for h in homes]
    bases = [min(r.ports.values()) for r in recs]  # type: ignore[attr-defined]
    pg_ports = [r.ports["postgres"] for r in recs]  # type: ignore[attr-defined]
    assert len(set(bases)) == len(homes), f"port-block collision: {bases}"  # pyright: ignore[reportUnknownArgumentType]
    assert len(set(pg_ports)) == len(homes), f"pg-port collision: {pg_ports}"  # pyright: ignore[reportUnknownArgumentType]


# ---------------------------------------------------------------------------
# require_installed_home — the settings-free `ava start` pure-bring-up gate
# (cli/preflight.py: must run BEFORE any settings-loading import, so it is
# exercised here with real files + env only — no settings monkeypatching).
# ---------------------------------------------------------------------------


def _patch_gate(
    monkeypatch: pytest.MonkeyPatch, *, home: Path, anchored: bool, registry: dict | None = None
):
    import json
    import os

    from shared import dotenv_boot

    monkeypatch.setattr(dotenv_boot, "resolve_ava_home", lambda: (home, anchored))
    reg = home.parent / "clusters.json"
    if registry is not None:
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text(json.dumps(registry))
    monkeypatch.setattr(os, "environ", {"AVA_CLUSTER_REGISTRY": str(reg)})


def _gate() -> int | None:
    from cli.preflight import require_installed_home

    return require_installed_home()


def _write_env(home: Path, text: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(text)


_GW_ENV = "AVA_MACHINE_SERVE_GATEWAY=true\nAVA_MACHINE_SERVE_AGENT_RUNNER=true\n"
_RUNNER_ENV = "AVA_MACHINE_SERVE_GATEWAY=false\nAVA_MACHINE_SERVE_AGENT_RUNNER=true\n"


def test_gate_passes_installed_gateway_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    home = tmp_path / ".ava-t"
    _write_env(home, _GW_ENV)
    _patch_gate(
        monkeypatch,
        home=home,
        anchored=True,
        registry={str(home): {"ports": {}, "gateway_home": str(home), "created_at": "t"}},
    )
    assert _gate() is None


def test_gate_refuses_unanchored_checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
    """A dev worktree that never ran the install (unanchored) is pointed at
    scripts/install.sh --worktree — start never births."""
    _patch_gate(monkeypatch, home=tmp_path / ".ava", anchored=False)
    assert _gate() == 1
    assert "--worktree" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_gate_refuses_uninstalled_gateway_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    """Anchored + gateway-capable but no registry record: point at install.sh."""
    home = tmp_path / ".ava"
    _write_env(home, _GW_ENV)
    _patch_gate(monkeypatch, home=home, anchored=True, registry={})
    assert _gate() == 1
    assert "install.sh" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_gate_runner_with_gateway_url_passes_without_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A pure agent-runner never has a registry record — its enrolled .env
    (carrying the gateway URL; connection facts are fetched at every start, not
    cached) is the installed marker."""
    home = tmp_path / ".ava"
    _write_env(home, _RUNNER_ENV + "AVA_GATEWAY_URL=https://gw\n")
    _patch_gate(monkeypatch, home=home, anchored=True)
    assert _gate() is None


def test_gate_runner_without_gateway_url_points_at_enroll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    """A runner .env without AVA_GATEWAY_URL was never enrolled — the gate
    points at `ava enroll` (connection facts are NOT the marker anymore: since
    the 2026-08-01 refactor an enrolled runner's .env deliberately has none)."""
    home = tmp_path / ".ava"
    _write_env(home, _RUNNER_ENV + "AVA_DB_URL=postgresql://ava:s@gw:5433/ava\n")
    _patch_gate(monkeypatch, home=home, anchored=True)
    assert _gate() == 1
    assert "ava enroll" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_gate_runner_without_env_points_at_enroll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    home = tmp_path / ".ava"
    home.mkdir()
    (home / "machine_serve_agent_runner").write_text("true")
    _patch_gate(monkeypatch, home=home, anchored=True)
    assert _gate() == 1
    assert "ava enroll" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_gate_unknown_role_without_record_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    """Roles unresolved (fresh host, no flags anywhere) + no record: point at the
    install paths rather than proceeding to a bring-up that cannot work."""
    home = tmp_path / ".ava"
    home.mkdir()
    _patch_gate(monkeypatch, home=home, anchored=True)
    assert _gate() == 1
    assert "install.sh" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_gate_reads_legacy_name_keyed_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A not-yet-migrated (name-keyed) registry still marks the home installed —
    the gate matches records by their own gateway_home."""
    home = tmp_path / ".ava"
    _write_env(home, _GW_ENV)
    _patch_gate(
        monkeypatch,
        home=home,
        anchored=True,
        registry={
            "main": {"name": "main", "ports": {}, "gateway_home": str(home), "created_at": "t"}
        },
    )
    assert _gate() is None


# ---------------------------------------------------------------------------
# require_installed_home — the .env port block must still match its record
#
# A destroy frees a port block while leaving the home's `.env` naming it; the
# next birth can legitimately be handed that block. Two homes on the gateway-host
# ended up both claiming pg=18123 / gw=18112 that way (#1075). The registry is
# what makes port ownership true and it is not what a starting process reads —
# so the gate compares them.
# ---------------------------------------------------------------------------


def _registry_for(home: Path, ports: dict) -> dict:
    return {str(home): {"ports": ports, "gateway_home": str(home), "created_at": "t"}}


_BLOCK_ENV = (
    _GW_ENV + "AVA_GATEWAY_PORT=18112\n"
    "AVA_DB_URL=postgresql://ava:s@localhost:18123/ava\n"
    "AVA_REDIS_URL=redis://ava:s@localhost:18124/0\n"
)
_BLOCK_PORTS = {"gateway": 18112, "postgres": 18123, "redis": 18124}


def test_gate_passes_when_env_ports_match_the_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The ordinary case: `.env` was derived from this record, so every anchor
    agrees and the gate is transparent."""
    home = tmp_path / ".ava-live"
    _write_env(home, _BLOCK_ENV)
    _patch_gate(monkeypatch, home=home, anchored=True, registry=_registry_for(home, _BLOCK_PORTS))
    assert _gate() is None


def test_gate_refuses_env_claiming_a_reallocated_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    """The #1075 shape: this home's `.env` still names the block it was born
    with, but the registry has since allocated that home a different one (or
    handed the old one to another cluster). Starting would bind ports the
    registry has promised elsewhere — the exact isolation break the per-cluster
    home design exists to prevent."""
    home = tmp_path / ".ava-stale"
    _write_env(home, _BLOCK_ENV)
    _patch_gate(
        monkeypatch,
        home=home,
        anchored=True,
        registry=_registry_for(home, {"gateway": 18160, "postgres": 18171, "redis": 18172}),
    )
    assert _gate() == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "18112" in err and "18160" in err  # the gateway port, both sides
    assert "18123" in err and "18171" in err  # postgres, read out of AVA_DB_URL
    assert "18124" in err and "18172" in err  # redis, read out of AVA_REDIS_URL
    assert "install.sh" in err  # how to re-derive .env from the record


def test_gate_refuses_on_a_single_drifted_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    """One anchor is enough — a block that agrees on two of three is still not
    the block this home owns."""
    home = tmp_path / ".ava-drift"
    _write_env(home, _BLOCK_ENV)
    _patch_gate(
        monkeypatch,
        home=home,
        anchored=True,
        registry=_registry_for(home, {**_BLOCK_PORTS, "postgres": 18999}),
    )
    assert _gate() == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "postgres: .env says 18123, the registry allocated 18999" in err
    assert (
        err.count(".env says") == 1  # pyright: ignore[reportUnknownMemberType]
    )  # the two that agree are not reported as drift  # pyright: ignore[reportUnknownMemberType]


def test_gate_ignores_ports_the_record_does_not_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A stale env key the record does not name (AVA_PGBOUNCER_PORT is retired —
    the pooler port is a registry fact), and older records carrying no per-cluster
    pg/redis at all: a key on only one side is not drift — comparing it would
    refuse every pre-per-cluster-data-plane home."""
    home = tmp_path / ".ava-partial"
    _write_env(home, _BLOCK_ENV + "AVA_PGBOUNCER_PORT=18125\n")
    _patch_gate(
        monkeypatch, home=home, anchored=True, registry=_registry_for(home, {"gateway": 18112})
    )
    assert _gate() is None


def test_gate_accepts_db_url_carrying_the_pooler_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """F8b: with pooling on, AVA_DB_URL carries the pooler port (offset 13 of this
    cluster's own block) instead of the direct pg port. Both belong to this
    cluster's record, so the gate must not read the pooled URL as drift."""
    home = tmp_path / ".ava-pooled"
    _write_env(
        home,
        _GW_ENV + "AVA_GATEWAY_PORT=18112\n"
        "AVA_DB_URL=postgresql://ava:s@localhost:18125/ava\n"
        "AVA_REDIS_URL=redis://ava:s@localhost:18124/0\n",
    )
    _patch_gate(monkeypatch, home=home, anchored=True, registry=_registry_for(home, _BLOCK_PORTS))
    assert _gate() is None


def test_gate_refuses_db_url_on_someone_elses_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    """A pooled URL naming a port OUTSIDE this cluster's block is still drift —
    the pooler-port tolerance is scoped to this record's own derived pooler port."""
    home = tmp_path / ".ava-foreign"
    _write_env(
        home,
        _GW_ENV + "AVA_GATEWAY_PORT=18112\n"
        "AVA_DB_URL=postgresql://ava:s@localhost:18113/ava\n"
        "AVA_REDIS_URL=redis://ava:s@localhost:18124/0\n",
    )
    _patch_gate(monkeypatch, home=home, anchored=True, registry=_registry_for(home, _BLOCK_PORTS))
    assert _gate() == 1
    err = capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]
    assert "18113" in err and "18123" in err  # claimed vs the record's postgres


def test_gate_ignores_an_env_that_names_no_ports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """An `.env` with no port-bearing keys claims nothing, so there is nothing to
    contradict — the record alone marks the home installed, as before."""
    home = tmp_path / ".ava-bare"
    _write_env(home, _GW_ENV)
    _patch_gate(monkeypatch, home=home, anchored=True, registry=_registry_for(home, _BLOCK_PORTS))
    assert _gate() is None


def test_gate_tolerates_urls_with_no_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A URL with no port (a unix socket / default-port form) claims no port
    rather than claiming 0 — reading it as a claim would refuse a legitimate
    home on a value it never stated."""
    home = tmp_path / ".ava-socket"
    _write_env(
        home,
        _GW_ENV + "AVA_GATEWAY_PORT=18112\nAVA_DB_URL=postgresql://ava@/ava\nAVA_REDIS_URL=\n",
    )
    _patch_gate(monkeypatch, home=home, anchored=True, registry=_registry_for(home, _BLOCK_PORTS))
    assert _gate() is None


def test_gate_tolerates_an_unparseable_url_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A malformed port in a URL is not drift — Settings rejects it far more
    loudly a moment later, and the gate must not turn "unparseable" into a
    port-ownership accusation."""
    home = tmp_path / ".ava-malformed"
    _write_env(home, _GW_ENV + "AVA_GATEWAY_PORT=18112\nAVA_DB_URL=postgresql://ava@h:notaport/a\n")
    _patch_gate(monkeypatch, home=home, anchored=True, registry=_registry_for(home, _BLOCK_PORTS))
    assert _gate() is None


def test_gate_skips_the_port_check_for_an_enrolled_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """An enrolled agent-runner has no record of its own — its connection facts
    come from the gateway at every start — so it must keep passing on its
    bootstrap env alone (AVA_GATEWAY_URL; no cached connection facts since the
    2026-08-01 refactor) and never be measured against a record it cannot have."""
    home = tmp_path / ".ava-runner"
    _write_env(home, _RUNNER_ENV + "AVA_GATEWAY_URL=https://gw:8000\n")
    _patch_gate(monkeypatch, home=home, anchored=True, registry={})
    assert _gate() is None


# ---------------------------------------------------------------------------
# cmd_cluster_ls
# ---------------------------------------------------------------------------


def test_cluster_ls_empty(isolated_registry: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from cli.commands.cluster_lifecycle import cmd_cluster_ls

    rc = cmd_cluster_ls()
    assert rc == 0
    assert "(no clusters registered)" in capsys.readouterr().out


def test_cluster_ls_shows_label_and_home(
    isolated_registry: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from cli.commands.cluster_lifecycle import cmd_cluster_ls

    home = tmp_path / ".ava-mycluster"
    cl.save_record(ClusterRecord(ports=_full_ports(18000), gateway_home=str(home), created_at="t"))
    rc = cmd_cluster_ls()
    assert rc == 0
    out = capsys.readouterr().out
    assert ".ava-mycluster" in out  # display label = home basename
    assert str(home) in out
    assert "18000" in out and "18011" in out


# ---------------------------------------------------------------------------
# cmd_cluster_down / destroy — addressed by path
# ---------------------------------------------------------------------------


def test_cluster_down_unknown_path(
    isolated_registry: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from cli.commands.cluster_lifecycle import cmd_cluster_down

    rc = cmd_cluster_down(path=str(tmp_path / ".ava-nope"))
    assert rc == 1
    assert "registry" in capsys.readouterr().err


def test_cluster_down_happy_path(
    isolated_registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cmd_cluster_down invokes the stop subprocess with AVA_HOME = the target home."""
    import cli.commands.cluster_lifecycle as gw

    home = tmp_path / ".ava-myc"
    cl.save_record(ClusterRecord(ports=_full_ports(19000), gateway_home=str(home), created_at="t"))

    captured: dict = {}

    class _FakeResult:
        returncode = 42

    def _fake_run(cmd, *, cwd, env, check):
        captured["cmd"] = cmd
        captured["env"] = env
        return _FakeResult()

    monkeypatch.setattr(gw.subprocess, "run", _fake_run)  # pyright: ignore[reportUnknownArgumentType]

    rc = gw.cmd_cluster_down(path=str(home))

    # Return code is propagated from the subprocess result.
    assert rc == 42
    # The stop subprocess must see the cluster's home.
    assert captured["env"]["AVA_HOME"] == str(home)
    # The command must be the stop invocation, WITHOUT --keep-infra: the child's
    # home IS the target cluster, so its own pg/redis go down too (destroy's
    # --drop-db must never rmtree a live instance's data dirs).
    assert "stop" in captured["cmd"]
    assert "--keep-infra" not in captured["cmd"]
    # A cluster-down is a full teardown of that cluster: it must take the browser
    # session down too (otherwise destroying a cluster leaves an orphan headed Chrome).
    assert "--stop-browser" in captured["cmd"]


def test_subprocess_env_isolates_cluster_and_identity_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cluster-down child must NOT inherit this process's cluster/identity env
    — the child's own $AVA_HOME/.env is authoritative."""
    import os

    import cli.commands.cluster_lifecycle as gw

    monkeypatch.setattr(
        os,
        "environ",
        {
            "AVA_DB_URL": "postgresql://prod@prodhost:5432/ava",
            "AVA_REDIS_URL": "redis://prodhost:6379/0",
            "AVA_MACHINE_SERVE_GATEWAY": "true",
            "AVA_MACHINE_NAME": "test-host",
            "AVA_GATEWAY_URL": "https://ava.prod.example.com",
            "AVA_CONFIG_FETCH": "skip",  # a maintenance verb's lite opt-out
            "PATH": "/usr/bin",
        },
    )
    env = gw._subprocess_env(gateway_home=tmp_path / "h")
    for leaked in (
        "AVA_DB_URL",
        "AVA_REDIS_URL",
        "AVA_MACHINE_SERVE_GATEWAY",
        "AVA_MACHINE_NAME",
        "AVA_GATEWAY_URL",
    ):
        assert leaked not in env, f"{leaked} leaked into the cluster subprocess env"
    assert env["AVA_HOME"] == str(tmp_path / "h")
    # The child runs THIS checkout's code against ANOTHER home — the shape
    # resolve_ava_home() refuses. Deliberate here, so the verb has to say so or
    # `ava cluster down/destroy` aborts whenever the calling checkout is anchored.
    assert env["AVA_HOME_OVERRIDE"] == "1"
    assert env["PATH"] == "/usr/bin"  # unrelated env is preserved
    # No config-source pin (AVA_CONFIG_SOURCE is gone): the child (`ava stop`) is
    # a settings-lite verb — cli.main opts it out of the gateway fetch itself.
    # (AVA_CONFIG_FETCH is not in the strip sets and may ride along; harmless —
    # the child is lite regardless.)
    assert "AVA_CONFIG_SOURCE" not in env
    assert "AVA_CONFIG_FETCH" in env  # preserved like any other AVA_* var


def test_destroy_removes_registry_record(
    isolated_registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cli.commands.cluster_lifecycle as gw

    home = tmp_path / ".ava-destroyme"
    cl.save_record(ClusterRecord(ports=_full_ports(18016), gateway_home=str(home), created_at="t"))
    monkeypatch.setattr(gw, "cmd_cluster_down", lambda *, path: 0)  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]

    rc = gw.cmd_cluster_destroy(path=str(home))
    assert rc == 0
    assert get_record(home) is None


def test_destroy_refuses_default_home(
    isolated_registry: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Destroying the default home (~/.ava, prod) is refused; returns 1."""
    from cli.commands.cluster_lifecycle import cmd_cluster_destroy

    rc = cmd_cluster_destroy(path="~/.ava")
    assert rc == 1
    assert "refusing" in capsys.readouterr().err


def test_destroy_unknown_path_returns_1(
    isolated_registry: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from cli.commands.cluster_lifecycle import cmd_cluster_destroy

    rc = cmd_cluster_destroy(path=str(tmp_path / ".ava-doesnotexist"))
    assert rc == 1
    assert "registry" in capsys.readouterr().err


def test_destroy_leaves_the_home_env_untouched(
    isolated_registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Freeing a port block must not be a credential-losing verb.

    `.env` is the only copy of this cluster's secret, of any key hand-added
    beyond `seed_allowlist()`, and — on an enrolled runner — of the bootstrap facts
    its gateway auth rides on. It also carries the connection URLs the
    data-plane identity is read from. Pinned byte-for-byte because "destroy
    should tidy up the .env too" is a permanently tempting change.

    Note what this is NOT: losing the secret would not strand the preserved pg
    data dirs. `ensure_cluster_role` re-sets the role password to the current
    secret on every bring-up as the initdb superuser over the private
    loopback-`trust` socket, so a fresh secret self-heals. The cost is
    credentials and config, not data.
    """
    import cli.commands.cluster_lifecycle as gw

    home = tmp_path / ".ava-keepenv"
    home.mkdir()
    env_file = home / ".env"
    body = "AVA_CLUSTER_SECRET=only-copy-of-this\nAVA_GATEWAY_PORT=18016\n"
    env_file.write_text(body)
    cl.save_record(ClusterRecord(ports=_full_ports(18016), gateway_home=str(home), created_at="t"))
    monkeypatch.setattr(gw, "cmd_cluster_down", lambda *, path: 0)  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]

    assert gw.cmd_cluster_destroy(path=str(home)) == 0
    assert get_record(home) is None  # the slot IS freed
    assert env_file.read_text() == body  # the credentials are NOT


def test_destroy_drop_db_removes_data_dirs_but_still_keeps_env(
    isolated_registry: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--drop-db` is the one destructive path, and it is scoped to the data
    dirs: the cluster's own pg/redis instance IS its database, so removing those
    directories is the drop (there is no shared server to DROP DATABASE inside).
    `.env` survives even here — the secret is what a later re-install reuses."""
    import cli.commands.cluster_lifecycle as gw

    home = tmp_path / ".ava-dropme"
    (home / "pg").mkdir(parents=True)
    (home / "redis").mkdir(parents=True)
    (home / "pg" / "PG_VERSION").write_text("17\n")
    (home / ".env").write_text("AVA_CLUSTER_SECRET=s\n")
    cl.save_record(ClusterRecord(ports=_full_ports(18032), gateway_home=str(home), created_at="t"))
    monkeypatch.setattr(gw, "cmd_cluster_down", lambda *, path: 0)  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]

    assert gw.cmd_cluster_destroy(path=str(home), drop_db=True) == 0
    assert not (home / "pg").exists()
    assert not (home / "redis").exists()
    assert (home / ".env").exists()


def test_destroy_cannot_reach_a_home_with_no_record(
    isolated_registry: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Why the gate's registry check is sufficient, stated as a test.

    The worry it answers: `require_installed_home`'s runner-only branch never
    consults the registry, so if a destroyed home could be runner-only, the
    leftover would boot. It cannot. `install_cluster` only births a record for a
    role containing `gateway` (a runner-only install writes serve flags and
    returns), and destroy refuses any home without one — so every home that can
    ever be destroyed is gateway-capable, which is exactly the population the
    registry branch covers. Both homes in the #1075 census carry
    `AVA_MACHINE_SERVE_GATEWAY=true`, consistent with that.
    """
    from cli.commands.cluster_lifecycle import cmd_cluster_destroy

    home = tmp_path / ".ava-runner-only"
    home.mkdir()
    (home / ".env").write_text(
        "AVA_MACHINE_SERVE_GATEWAY=false\n"
        "AVA_MACHINE_SERVE_AGENT_RUNNER=true\n"
        "AVA_DB_URL=postgresql://ava:s@gw:5433/ava\n"
    )
    assert cmd_cluster_destroy(path=str(home)) == 1
    assert "registry" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# OS-scheduled job cleanup
# ---------------------------------------------------------------------------
# Freeing the registry slot without deregistering the cluster's launchd /
# crontab entries leaves jobs pointing at a home that no longer has a cluster —
# and for a worktree, at a checkout about to be deleted, so they fail every
# interval forever.
#
# These tests drive the REAL `shared.os_*` helpers and stub only the bottom
# platform layer, so what is asserted is the slug those helpers actually
# resolved — not that some intermediate variable was set.


class _RecordingBackend:
    """Stands in for the platform backend: records the job identity each
    unregister resolved to, without touching launchd / crontab."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, str]] = []  # (kind, slug)

    def unregister_cron(self, slug: str) -> None:
        self.jobs.append(("health-probe", slug))

    def unregister_autostart(self, slug: str) -> None:
        self.jobs.append(("autostart", slug))

    def unregister_logs_job(self, slug: str) -> None:
        self.jobs.append(("logs-maintenance", slug))

    def unregister_watchdog_probe(self, role: str, slug: str) -> None:
        self.jobs.append((f"watchdog-probe.{role}", slug))


@pytest.fixture()
def recording_backend(monkeypatch: pytest.MonkeyPatch) -> _RecordingBackend:
    backend = _RecordingBackend()
    monkeypatch.setattr("shared.platform_backend.get_backend", lambda: backend)
    return backend


def _registered_home(tmp_path: Path, name: str, base: int) -> Path:
    home = tmp_path / name
    cl.save_record(ClusterRecord(ports=_full_ports(base), gateway_home=str(home), created_at="t"))
    return home


def test_destroy_unregisters_every_scheduled_job(
    isolated_registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recording_backend: _RecordingBackend,
) -> None:
    import cli.commands.cluster_lifecycle as gw

    home = _registered_home(tmp_path, ".ava-jobs", 18017)
    monkeypatch.setattr(gw, "cmd_cluster_down", lambda *, path: 0)  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]

    assert gw.cmd_cluster_destroy(path=str(home)) == 0
    # Both capabilities' probes, not just one — a single box registered two.
    assert sorted(kind for kind, _slug in recording_backend.jobs) == [
        "autostart",
        "health-probe",
        "logs-maintenance",
        "watchdog-probe.agent-runner",
        "watchdog-probe.gateway",
    ]


def test_destroy_unregisters_the_target_homes_jobs_not_this_processs(
    isolated_registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recording_backend: _RecordingBackend,
) -> None:
    """The job names destroy removes must be the TARGET cluster's.

    `settings` is built once at import, so the target cannot be signalled by
    assigning `os.environ["AVA_HOME"]` mid-process: the helpers would resolve
    this process's own home and `ava cluster destroy --path <worktree>` run from
    the prod checkout would deregister prod's health probe, both watchdog probes
    and autostart.
    """
    import cli.commands.cluster_lifecycle as gw
    from shared.os_autostart import _autostart_label
    from shared.os_cron import _health_probe_label
    from shared.os_watchdog_probe import probe_label
    from shared.paths import ava_home

    home = _registered_home(tmp_path, ".ava-target", 18018)
    monkeypatch.setattr(gw, "cmd_cluster_down", lambda *, path: 0)  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]
    # A decoy: were the target ignored, this is the home the helpers would fall
    # back to (in prod, that is ~/.ava).
    this_process = cl.home_slug(ava_home())
    target = cl.home_slug(home)
    assert target != this_process

    assert gw.cmd_cluster_destroy(path=str(home)) == 0

    resolved = dict(recording_backend.jobs)
    assert set(resolved.values()) == {target}
    # Spelled out as the launchd labels the slug produces, so the assertion is
    # about the job actually removed rather than an opaque token.
    assert _health_probe_label(resolved["health-probe"]) == f"com.ava.{target}.health-probe"
    assert _autostart_label(resolved["autostart"]) == f"com.ava.{target}.autostart"
    assert (
        probe_label("gateway", resolved["watchdog-probe.gateway"])
        == f"com.ava.{target}.watchdog-probe.gateway"
    )
    assert f"com.ava.{this_process}." not in str(recording_backend.jobs)


def test_destroy_reports_a_failing_unregister_but_still_succeeds(
    isolated_registry: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A half-registered cluster (or a host whose scheduler is unavailable) must
    still be destroyable — the registry slot matters more than a stale job — but
    the failure is reported, never printed as a success."""
    import cli.commands.cluster_lifecycle as gw

    home = _registered_home(tmp_path, ".ava-broken", 18019)
    monkeypatch.setattr(gw, "cmd_cluster_down", lambda *, path: 0)  # noqa: ARG005  # pyright: ignore[reportUnknownArgumentType]

    def _boom(_slug: str) -> None:
        raise RuntimeError("launchctl unavailable")

    backend = _RecordingBackend()
    monkeypatch.setattr(backend, "unregister_cron", _boom)
    monkeypatch.setattr("shared.platform_backend.get_backend", lambda: backend)

    assert gw.cmd_cluster_destroy(path=str(home)) == 0
    assert get_record(home) is None
    captured = capsys.readouterr()
    assert "launchctl unavailable" in captured.err
    assert "OS-scheduled jobs (health probe" not in captured.out


# ---------------------------------------------------------------------------
# _ensure_pgvector_extension — remote-managed planes skip (no local admin socket)
# ---------------------------------------------------------------------------


def test_ensure_pgvector_extension_skips_remote_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import types

    import cli.commands.cluster_lifecycle as gw

    called: list[tuple[str, str]] = []

    def fake_ensure(identity: str, *, base_admin_url: str) -> None:
        called.append((identity, base_admin_url))

    # is_remote is a computed pydantic property (no setter), so the wrapper's
    # module-level settings reference is swapped wholesale.
    monkeypatch.setattr(
        gw,
        "settings",
        types.SimpleNamespace(data_plane=types.SimpleNamespace(is_remote=True)),
    )
    monkeypatch.setattr(cl, "ensure_pgvector_extension", fake_ensure)
    gw._ensure_pgvector_extension("ava_ident", base_admin_url="postgresql://admin@/postgres")
    assert called == []


def test_ensure_pgvector_extension_forwards_on_local_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import types

    import cli.commands.cluster_lifecycle as gw

    called: list[tuple[str, str]] = []

    def fake_ensure(identity: str, *, base_admin_url: str) -> None:
        called.append((identity, base_admin_url))

    monkeypatch.setattr(
        gw,
        "settings",
        types.SimpleNamespace(data_plane=types.SimpleNamespace(is_remote=False)),
    )
    monkeypatch.setattr(cl, "ensure_pgvector_extension", fake_ensure)
    gw._ensure_pgvector_extension("ava_ident", base_admin_url="postgresql://admin@/postgres")
    assert called == [("ava_ident", "postgresql://admin@/postgres")]
