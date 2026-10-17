"""The cluster's own redis bring-up never puts secret material on a command line (issue #974).

`--requirepass <secret>` and `redis-cli -a <secret>` would both publish the cluster secret to any
local `ps`; it goes through a 0600 conf file and `$REDISCLI_AUTH` instead. The companion
launchers (session launch, schedule processes, agent shells) are in
`tests/components/base/test_no_secrets_on_argv.py`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from base.cluster import ownership, port_preflight
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.sessions.pty.tests.secret_argv import SECRET, assert_clean
from cli.commands.data_plane import cluster_instance as ci
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.tests.data_plane_records import with_redis_admin


def test_redis_bringup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    machine_identity: MachineIdentity,
    data_plane_config: DataPlaneConfig,
) -> None:
    """The cluster's own redis: `--requirepass <secret>` and `redis-cli -a <secret>`
    would both publish the cluster secret. It goes through a 0600 conf file and
    `$REDISCLI_AUTH` instead."""
    calls: list[list[str]] = []

    def fake_run(args: Any, **_kw: Any) -> Any:
        calls.append([str(a) for a in cast(list[object] | tuple[object, ...], args)])
        return subprocess.CompletedProcess(args, returncode=0, stdout="PONG", stderr="")

    monkeypatch.setattr(ci.subprocess, "run", fake_run)
    monkeypatch.setattr(ownership, "redis_data_dir", lambda: tmp_path / "redis")
    monkeypatch.setattr(ci, "ensure_redis_acl", lambda *_a, **_k: 0)  # pyright: ignore[reportUnknownArgumentType]

    def bind_fixture(*, machine: MachineIdentity) -> list[str]:
        assert machine is machine_identity
        return ["127.0.0.1"]

    monkeypatch.setattr(port_preflight, "bind_addrs", bind_fixture)
    monkeypatch.setattr(ci, "print", lambda *_a, **_k: None, raising=False)  # pyright: ignore[reportUnknownArgumentType]
    # `redis_server_bin` resolves through `brew_prefix`, which shells out to `brew`; the faked
    # `subprocess.run` above would answer it with "PONG". Stub the resolved path directly.
    monkeypatch.setattr(
        ci,
        "redis_server_bin",
        lambda _config: "/opt/homebrew/opt/redis@8.2/bin/redis-server",  # pyright: ignore[reportUnknownArgumentType]
    )

    # down on the first probe (so the full start path runs), up on the next
    probes = iter([False, True, True])
    monkeypatch.setattr(ci, "redis_running", lambda *_a: next(probes))  # pyright: ignore[reportUnknownArgumentType]
    machine_identity.set(host="127.0.0.1")
    config = with_redis_admin(data_plane_config, SECRET)
    assert ci.start_redis(config, 46999, SECRET, "meiva", machine=machine_identity) == 0

    for argv in calls:
        assert_clean(argv, label="redis bring-up")
    conf = tmp_path / "redis" / "redis.conf"
    assert conf.stat().st_mode & 0o777 == 0o600
    assert SECRET in conf.read_text()
