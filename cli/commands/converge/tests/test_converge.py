from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from base.config import ConfigBoot
from cli.commands.converge import _frontend_env as _fe_env
from cli.commands.converge import _steps
from cli.commands.converge import host as converge_host


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    return tmp_path


def _ctx(repo: Path, ava_home: Path, roles=None):
    return converge_host.ConvergeCtx(repo=repo, ava_home=ava_home, roles=roles, config=ConfigBoot())  # pyright: ignore[reportUnknownArgumentType]


def test_ensure_ava_on_path_links_bare_ava_to_this_checkouts_cli(
    home: Path, tmp_path: Path
) -> None:
    link = home / ".local" / "bin" / "ava"
    link.parent.mkdir(parents=True)
    repo = tmp_path / "repo"
    link.symlink_to(repo / "scripts" / "ava-launcher.sh")  # the retired launcher link
    converge_host._ensure_ava_on_path(_ctx(repo, home))
    assert link.readlink() == repo / ".venv" / "bin" / "ava"
    converge_host._ensure_ava_on_path(_ctx(repo, home))  # idempotent
    assert link.readlink() == repo / ".venv" / "bin" / "ava"


def test_ensure_local_bin_on_path_block_is_idempotent(home, tmp_path: Path):
    ctx = _ctx(tmp_path, home)  # pyright: ignore[reportUnknownArgumentType]
    rc = home / ".zshrc"
    rc.write_text("export FOO=1\n")  # pyright: ignore[reportUnknownMemberType]

    converge_host._ensure_local_bin_on_path(ctx)
    converge_host._ensure_local_bin_on_path(ctx)

    text = rc.read_text()  # pyright: ignore[reportUnknownMemberType]
    assert text.count(converge_host._PATH_BEGIN) == 1  # pyright: ignore[reportUnknownMemberType]
    assert "export FOO=1" in text
    assert str(home / ".local" / "bin") in text  # pyright: ignore[reportUnknownArgumentType]


def test_ensure_ava_home_dirs(home, tmp_path: Path):
    ava_home = tmp_path / "avahome"
    converge_host._ensure_ava_home_dirs(_ctx(tmp_path, ava_home))
    for sub in ("logs", "configs", "secrets"):
        assert (ava_home / sub).is_dir()
    # Spotlight exclusion marker: the logs dir holds high-churn rotating logs
    # that mds_stores would otherwise index (multi-GB RSS on this box).
    assert (ava_home / "logs" / ".metadata_never_index").is_file()


def test_ensure_ava_home_dirs_recursively_converges_private_data_trees(home, tmp_path: Path):
    ava_home = tmp_path / "avahome"
    targets = (
        ava_home / "logs" / "daemon" / "current.log",
        ava_home / "workspaces" / "7" / "download.txt",
        ava_home / "memory" / ".git" / "config",
    )
    for target in targets:
        target.parent.mkdir(parents=True)
        target.write_text("private")
        target.chmod(0o644)
        for parent in (target.parent, target.parent.parent):
            parent.chmod(0o755)

    converge_host._ensure_ava_home_dirs(_ctx(tmp_path, ava_home))

    for target in targets:
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(target.parent.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="unix sockets are POSIX-only")
def test_ensure_ava_home_dirs_survives_a_workspace_socket(tmp_path: Path):
    """A dead workspace socket must not abort the dir-skeleton step.

    The 2026-09-12 host outage: converge raised on a leftover app.sock and the
    updater exited rc=1 before its start step. macOS needs a short root under
    /tmp for the ~104-byte AF_UNIX path limit; pytest's tmp_path is too long.
    """
    import shutil
    import socket
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="ava-converge-hd-", dir="/tmp"))
    ava_home = root / "avahome"
    socket_dir = ava_home / "workspaces" / "6063" / "f13b-poc"
    socket_dir.mkdir(parents=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        socket_path = socket_dir / "app.sock"
        server.bind(str(socket_path))

        converge_host._ensure_ava_home_dirs(_ctx(root, ava_home))

        assert stat.S_ISSOCK(socket_path.lstat().st_mode)  # left in place
        assert stat.S_IMODE((ava_home / "workspaces").stat().st_mode) == 0o700
        assert (ava_home / "logs" / ".metadata_never_index").exists()
    finally:
        server.close()
        shutil.rmtree(root)


def test_ensure_ava_home_dirs_rejects_logs_symlink_before_writing_marker(home, tmp_path: Path):
    ava_home = tmp_path / "avahome"
    outside = tmp_path / "outside"
    outside.mkdir()
    ava_home.mkdir()
    (ava_home / "logs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match=r"logs.*symlink"):
        converge_host._ensure_ava_home_dirs(_ctx(tmp_path, ava_home))

    assert not (outside / ".metadata_never_index").exists()


def test_converge_host_runs_universal_and_skips_unit_state_when_role_none(home, tmp_path: Path):
    calls: list[str] = []
    steps = (
        converge_host.ConvergeStep("wiring", lambda _: calls.append("wiring")),
        converge_host.ConvergeStep(
            "unit", lambda _: calls.append("unit"), requires_unit_config=True
        ),
    )
    converge_host.converge_host(tmp_path, None, ava_home=home, steps=steps)  # pyright: ignore[reportUnknownArgumentType]
    assert calls == ["wiring"]  # unit-state deferred when role is None


def test_converge_host_filters_by_role(home, tmp_path: Path):
    calls: list[str] = []
    steps = (
        converge_host.ConvergeStep(
            "cp-only",
            lambda _: calls.append("cp"),
            roles=frozenset({"gateway"}),
        ),
        converge_host.ConvergeStep("both", lambda _: calls.append("both")),
    )
    converge_host.converge_host(tmp_path, frozenset({"agent-runner"}), ava_home=home, steps=steps)  # pyright: ignore[reportUnknownArgumentType]
    assert calls == ["both"]  # gateway-only step skipped on agent-runner


def test_converge_host_skips_host_global_for_dev_cluster(
    home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A dev (non-default-home) cluster must NOT run host-global wiring (the symlink /
    shell-rc edit) — those belong to the host's prod install, not a worktree."""
    monkeypatch.setattr(converge_host, "is_default_home", lambda _h: False)  # pyright: ignore[reportUnknownArgumentType]
    calls: list[str] = []
    steps = (
        converge_host.ConvergeStep(
            "hostwide", lambda _: calls.append("hostwide"), host_global=True
        ),
        converge_host.ConvergeStep("percluster", lambda _: calls.append("percluster")),
    )
    converge_host.converge_host(tmp_path, frozenset({"gateway"}), ava_home=home, steps=steps)  # pyright: ignore[reportUnknownArgumentType]
    assert calls == ["percluster"]  # host-global skipped


def test_converge_host_runs_host_global_for_default_cluster(
    home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The prod default home (~/.ava, non-worktree repo) DOES run host-global wiring."""
    monkeypatch.setattr(converge_host, "is_default_home", lambda _h: True)  # pyright: ignore[reportUnknownArgumentType]
    calls: list[str] = []
    steps = (
        converge_host.ConvergeStep(
            "hostwide", lambda _: calls.append("hostwide"), host_global=True
        ),
        converge_host.ConvergeStep("percluster", lambda _: calls.append("percluster")),
    )
    converge_host.converge_host(tmp_path, frozenset({"gateway"}), ava_home=home, steps=steps)  # pyright: ignore[reportUnknownArgumentType]
    assert calls == ["hostwide", "percluster"]


def test_host_wiring_leaves_existing_editable_install_unchanged(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host setup has no automatic source repair or package reinstall authority."""
    source = tmp_path / "source"
    site = source / ".venv" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    pointer = site / "_editable_impl_ava.pth"
    pointer.write_text(str(tmp_path / "deleted-checkout"))
    scripts = source / ".venv" / "bin"
    scripts.mkdir()
    files = {pointer: pointer.read_bytes()}
    modes = {path: stat.S_IMODE(path.stat().st_mode) for path in (site, scripts, pointer)}
    monkeypatch.setattr("base.deploy.git.cluster_drift.prod_source_dir", lambda: source)

    def default_home(_home: Path) -> bool:
        return True

    monkeypatch.setattr(converge_host, "is_default_home", default_home)

    def no_reinstall(*_args: object, **_kwargs: object) -> None:
        pytest.fail("host convergence cannot reinstall an editable package")

    monkeypatch.setattr("cli.python_install.install", no_reinstall)
    steps = tuple(
        step
        for step in converge_host.CONVERGE_STEPS
        if step.host_global and step.apply.__module__ == _steps.__name__
    )
    converge_host.converge_host(source, None, ava_home=home, steps=steps, services=frozenset())
    assert {path: path.read_bytes() for path in files} == files
    assert {path: stat.S_IMODE(path.stat().st_mode) for path in modes} == modes
    assert not (scripts / "ava").exists()
    assert (home / ".local" / "bin" / "ava").is_symlink()


@pytest.mark.parametrize("worktree_parent", [".claude/worktrees", ".worktrees"])
def test_converge_host_skips_host_global_in_worktree_even_if_cluster_default(
    home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worktree_parent
):
    """Fail-open guard: an uninstalled dev worktree's home resolution falls back to
    ~/.ava (the default home), but a repo under .worktrees/ or .claude/worktrees/
    is a dev worktree — host-global must still be skipped so a bare
    `ava start`/`converge` in a worktree never repoints the prod symlink."""
    monkeypatch.setattr(converge_host, "is_default_home", lambda _h: True)  # pyright: ignore[reportUnknownArgumentType]
    wt_repo = tmp_path / worktree_parent / "feat-x"
    wt_repo.mkdir(parents=True)  # pyright: ignore[reportUnknownMemberType]
    calls: list[str] = []
    steps = (
        converge_host.ConvergeStep(
            "hostwide", lambda _: calls.append("hostwide"), host_global=True
        ),
        converge_host.ConvergeStep("percluster", lambda _: calls.append("percluster")),
    )
    converge_host.converge_host(wt_repo, frozenset({"gateway"}), ava_home=home, steps=steps)  # pyright: ignore[reportUnknownArgumentType]
    assert calls == ["percluster"]  # host-global skipped despite cluster == default


def _capable_helper_ctx(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A host the capability probe clears, with an empty .env."""
    from base.config import settings

    ava_home = tmp_path / "avahome"
    ava_home.mkdir()
    (ava_home / ".env").write_text("")
    monkeypatch.setattr(converge_host.sys, "platform", "darwin")
    monkeypatch.setattr("base.host.system.probes.permissions_helper_incapability", lambda: None)
    monkeypatch.setattr(settings.services, "permissions_helper_enabled", True)
    return _ctx(tmp_path, ava_home)


def test_permissions_helper_step_refuses_when_this_process_cannot_sign(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """No launch can bypass the required signed ancestor after signing fails."""
    from services.desktop.permissions_helper.lifecycle import (
        PermissionsHelperSigningUnavailableError,
    )

    ctx = _capable_helper_ctx(monkeypatch, tmp_path)

    def cannot_sign() -> None:
        raise PermissionsHelperSigningUnavailableError("the login keychain is not unlocked")

    monkeypatch.setattr("services.desktop.permissions_helper.converge", cannot_sign)
    with pytest.raises(PermissionsHelperSigningUnavailableError, match="keychain"):
        converge_host._ensure_permissions_helper(ctx)


def test_permissions_helper_step_still_aborts_on_a_real_build_defect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Only the unreachable-key case is downgraded. A capable host that fails to
    compile or load the helper is a genuine defect and still aborts converge."""
    from services.desktop.permissions_helper.lifecycle import PermissionsHelperBuildError

    ctx = _capable_helper_ctx(monkeypatch, tmp_path)

    def _boom() -> None:
        raise PermissionsHelperBuildError("swiftc failed (1): syntax error")

    monkeypatch.setattr("services.desktop.permissions_helper.converge", _boom)

    with pytest.raises(PermissionsHelperBuildError, match="swiftc failed"):
        converge_host._ensure_permissions_helper(ctx)


def test_converge_host_fail_fast_reraises(home, tmp_path: Path):
    def boom(ctx):
        raise RuntimeError("nope")

    steps = (converge_host.ConvergeStep("boom", boom),)  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(RuntimeError, match="nope"):
        converge_host.converge_host(tmp_path, frozenset({"gateway"}), ava_home=home, steps=steps)  # pyright: ignore[reportUnknownArgumentType]


def test_converge_host_runs_in_order(home, tmp_path: Path):
    calls: list[str] = []
    steps = (
        converge_host.ConvergeStep("first", lambda _: calls.append("first")),
        converge_host.ConvergeStep("second", lambda _: calls.append("second")),
    )
    converge_host.converge_host(tmp_path, frozenset({"gateway"}), ava_home=home, steps=steps)  # pyright: ignore[reportUnknownArgumentType]
    assert calls == ["first", "second"]


@pytest.mark.parametrize("maintenance_held", [False, True])
def test_cmd_converge_unconfigured_returns_zero(
    home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maintenance_held: bool
):
    import cli.commands._repo as _repo_commands
    from base.cluster.dataplane import runtime_binaries as rb

    repo = tmp_path / "repo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "ava").write_text("#!/bin/sh\n")
    monkeypatch.setenv("AVA_HOME", str(home / "avahome"))  # pyright: ignore[reportUnknownArgumentType]
    # A unit test must not reach Maven Central: seed the vendored Postgres tree so
    # the vendored-binaries step takes ensure_pg_binaries()'s idempotent early
    # return (the real download is covered by base/cluster/dataplane/tests/test_vendored_binaries.py).
    seeded_bin = rb.vendored_pg_dir() / "bin"
    seeded_bin.mkdir(parents=True)
    (seeded_bin / "initdb").write_text("#!/bin/sh\n")
    # The pgvector injection shares the step: seed its detection file too so it
    # takes the idempotent early return (the real injection is covered by
    # scripts/ci/pgvector_runtime_smoke.py).
    seeded_ext = rb.vendored_pg_dir() / "share/postgresql/extension"
    seeded_ext.mkdir(parents=True)
    (seeded_ext / rb._PGVECTOR_SQL).write_text("-- seeded\n")
    monkeypatch.setattr(_repo_commands, "_repo_root", lambda: repo)
    monkeypatch.setattr(_repo_commands, "_roles_or_none", lambda: None)

    # host-global wiring (the ava symlink) is prod-install only, so this test must
    # run as the default home, not the suite's ambient tmpfs home.
    monkeypatch.setattr(converge_host, "is_default_home", lambda _h: True)  # pyright: ignore[reportUnknownArgumentType]

    # Unconfigured converge must defer helper ancestry until identity exists.
    # Record that boundary explicitly; reaching it would be a contract failure.
    helper_calls: list[str] = []
    monkeypatch.setattr(
        "services.desktop.permissions_helper.converge", lambda: helper_calls.append("helper")
    )
    operation = None
    if maintenance_held:
        from datetime import UTC, datetime

        from base.deploy.maintenance import admission, pause_owner

        acquired_at = datetime(2026, 10, 8, tzinfo=UTC)
        pause_owner.begin_maintenance("converge", acquired_at)
        with pytest.raises(RuntimeError, match="cannot release"):
            converge_host.cmd_converge()
        operation = admission.authorized_start("converge", acquired_at)
    # The real lazy boot delivers the scratch unit environment during WAL-G's gate.
    with patch.dict(os.environ):
        rc = converge_host.cmd_converge(operation=operation)
    assert rc == 0
    assert helper_calls == []
    assert (home / ".local" / "bin" / "ava").is_symlink()  # pyright: ignore[reportUnknownMemberType]


def test_frontend_env_override_guard_passes_clean(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "ui" / "web").mkdir(parents=True)
    (repo / "ui" / "web" / ".env.development").write_text("# tracked, next-dev-only\n")
    ava_home = tmp_path / "avahome"
    ava_home.mkdir()
    # A unit .env carrying only AVA_* vars (the legitimate case) must pass.
    (ava_home / ".env").write_text("AVA_GATEWAY_PORT=8800\nAVA_CLUSTER=main\n")

    _fe_env.ensure_no_frontend_env_overrides(_ctx(repo, ava_home))  # must not raise


@pytest.mark.parametrize("name", _fe_env._FORBIDDEN_FRONTEND_ENV_FILES)
def test_frontend_env_override_guard_rejects_build_time_files(tmp_path: Path, name):
    """`next build` bakes NEXT_PUBLIC_* from these files into the bundle,
    silently beating the runtime gateway inference (2026-06-09 prod outage)."""
    repo = tmp_path / "repo"
    (repo / "ui" / "web").mkdir(parents=True)
    (repo / "ui" / "web" / name).write_text("NEXT_PUBLIC_API_BASE=https://dead.example\n")  # pyright: ignore[reportUnknownMemberType]

    with pytest.raises(RuntimeError, match="build-time env override"):
        _fe_env.ensure_no_frontend_env_overrides(_ctx(repo, tmp_path))


def test_frontend_env_override_guard_rejects_next_public_in_unit_env(tmp_path: Path):
    """A NEXT_PUBLIC_GATEWAY_PORT in the unit $AVA_HOME/.env is the 2026-06-23 prod
    outage root cause: load_ava_env loads the whole unit .env into os.environ, so a
    stale value (8800, a VPS port) baked into the bundle and broke login. NEXT_PUBLIC_*
    is derived + injected on the build command line, so it belongs nowhere in .env."""
    repo = tmp_path / "repo"
    (repo / "ui" / "web").mkdir(parents=True)
    ava_home = tmp_path / "avahome"
    ava_home.mkdir()
    (ava_home / ".env").write_text("AVA_GATEWAY_PORT=8000\nNEXT_PUBLIC_GATEWAY_PORT=8800\n")

    with pytest.raises(RuntimeError, match="NEXT_PUBLIC_GATEWAY_PORT"):
        _fe_env.ensure_no_frontend_env_overrides(_ctx(repo, ava_home))


@pytest.mark.parametrize(
    "line",
    [
        "NEXT_PUBLIC_GATEWAY_PORT=8800",
        "  NEXT_PUBLIC_GATEWAY_PORT=8800",  # leading whitespace
        "export NEXT_PUBLIC_API_BASE=https://x",  # `export ` prefix
    ],
)
def test_next_public_keys_detects_assignment_shapes(tmp_path: Path, line):
    env = tmp_path / ".env"
    env.write_text(f"AVA_CLUSTER=main\n{line}\n")
    assert _fe_env._next_public_keys_in_env_file(env)


def test_next_public_keys_ignores_comments_and_substrings(tmp_path: Path):
    """A commented-out line or a var that merely contains the substring must not trip."""
    env = tmp_path / ".env"
    env.write_text(
        "# NEXT_PUBLIC_GATEWAY_PORT=8800\n"  # comment, not an assignment
        "MY_NEXT_PUBLIC_THING=1\n"  # substring, not a NEXT_PUBLIC_* key
        "AVA_GATEWAY_PORT=8000\n"
    )
    assert _fe_env._next_public_keys_in_env_file(env) == []


def test_next_public_keys_absent_file_is_empty(tmp_path: Path):
    assert _fe_env._next_public_keys_in_env_file(tmp_path / "nope.env") == []


def _pgbouncer_ctx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, db_url: str | None, enabled: bool
):
    """Wire ensure_pgbouncer_step's deps: a record (pooler 6433 / pg 5433), settings
    reflecting the toggle, and an optional existing .env carrying the pre-cutover
    AVA_DB_URL."""
    from base import cluster

    rec = cluster.ClusterRecord(
        ports=cast(
            "cluster.ClusterPorts",
            {"gateway": 8000, "postgres": 5433, "redis": 6380, "pgbouncer": 6433},
        ),
        gateway_home=str(tmp_path),
        created_at="t",
    )
    monkeypatch.setattr(cluster, "get_record", lambda _home: rec)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(converge_host.settings.data_plane, "pgbouncer_enabled", enabled)
    ctx = _ctx(tmp_path / "repo", tmp_path)
    if db_url is not None:
        (tmp_path / ".env").write_text(f"AVA_DB_URL={db_url}\nAVA_PGBOUNCER_PORT=6433\n")
    return ctx


_DIRECT_URL = "postgresql://ava_main:sek@127.0.0.1:5433/ava_main"
_POOLED_URL = "postgresql://ava_main:sek@127.0.0.1:6433/ava_main"


def test_ensure_pgbouncer_step_migrates_direct_url_to_pooler_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The existing-.env migration path: a pre-F8b cluster's AVA_DB_URL carries
    the direct pg port; with the toggle on (default), converge rewrites it to the
    pooler port and drops the retired AVA_PGBOUNCER_PORT key."""
    ctx = _pgbouncer_ctx(tmp_path, monkeypatch, db_url=_DIRECT_URL, enabled=True)
    converge_host.ensure_pgbouncer_step(ctx)
    env = (tmp_path / ".env").read_text()
    assert "AVA_DB_URL=" + _POOLED_URL in env  # the record's pooler 6433
    assert "AVA_PGBOUNCER_PORT" not in env


def test_ensure_pgbouncer_step_leaves_remote_url_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A remote-managed data plane has no local pooler: converge must neither
    rewrite the provider's URL port nor preflight the local binary (Task
    #1752)."""
    remote_url = "postgresql://ava:sek@10.9.8.7:5432/ava"
    ctx = _pgbouncer_ctx(
        tmp_path,
        monkeypatch,
        db_url=remote_url,
        enabled=True,  # the pooler toggle is meaningless for a remote plane
    )
    monkeypatch.setattr(converge_host.settings.data_plane, "db_url", remote_url)
    monkeypatch.setattr(converge_host.settings.data_plane, "redis_url", "rediss://10.9.8.7:6380/0")
    converge_host.ensure_pgbouncer_step(ctx)
    env = (tmp_path / ".env").read_text()
    assert "AVA_DB_URL=" + remote_url in env, "the remote URL must pass through byte-identical"
    # The pooler port normalization (and the retired-key cleanup) is skipped
    # wholesale on the remote branch — the URL's port is the provider's.


def test_ensure_pgbouncer_step_rewrites_pooler_url_back_to_direct_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The kill-switch: toggle off + restart -> the pooler never starts and the
    URL is rewritten to the direct pg port."""
    ctx = _pgbouncer_ctx(tmp_path, monkeypatch, db_url=_POOLED_URL, enabled=False)
    converge_host.ensure_pgbouncer_step(ctx)
    env = (tmp_path / ".env").read_text()
    assert "AVA_DB_URL=" + _DIRECT_URL in env
    assert "AVA_PGBOUNCER_PORT" not in env


def test_ensure_pgbouncer_step_leaves_matching_url_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A URL that already matches the toggle is not rewritten — no snapshot churn
    every start — but the retired key is still dropped."""
    ctx = _pgbouncer_ctx(tmp_path, monkeypatch, db_url=_POOLED_URL, enabled=True)
    converge_host.ensure_pgbouncer_step(ctx)
    env = (tmp_path / ".env").read_text()
    assert "AVA_DB_URL=" + _POOLED_URL in env
    assert "AVA_PGBOUNCER_PORT" not in env


def test_ensure_pgbouncer_step_leaves_operator_standin_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A URL naming neither this cluster's pg nor its pooler port (a dev-only
    stand-in) is not rewritten — converge only normalizes the two cluster ports."""
    ctx = _pgbouncer_ctx(
        tmp_path, monkeypatch, db_url="postgresql://ava:dev@localhost:5432/ava", enabled=True
    )
    converge_host.ensure_pgbouncer_step(ctx)
    env = (tmp_path / ".env").read_text()
    assert "AVA_DB_URL=postgresql://ava:dev@localhost:5432/ava" in env
    assert "AVA_PGBOUNCER_PORT" not in env


def test_ensure_pgbouncer_step_without_env_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """No .env (a fresh home converge runs before birth materializes URLs): the
    step is a no-op, not a crash."""
    ctx = _pgbouncer_ctx(tmp_path, monkeypatch, db_url=None, enabled=True)
    converge_host.ensure_pgbouncer_step(ctx)
    assert not (tmp_path / ".env").exists()


def _rw_url(pw: str, *, host: str, user: str = "") -> str:
    """Build a credentialed redis URL from parts, so the source carries no
    `scheme://user:password@host` literal for a secret scanner to flag (same
    convention as base/tests/test_url_secret.py) — every value is a throwaway
    fixture, not a real credential."""
    return f"redis://{user}:{pw}@{host}/0"


def _rw_pg_url(pw: str, *, host: str, user: str = "ava_main") -> str:
    """The postgresql twin of _rw_url (parts-built, scanner-safe)."""
    return f"postgresql://{user}:{pw}@{host}/ava_main"


# --- health-port backfill value decoding (#2704) ---------------------------


def _screen_capture_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, enabled=True, incapability=None
):
    from base.config import settings

    monkeypatch.setattr("base.host.converge.screen_capture.ava_home", lambda: tmp_path)
    monkeypatch.setattr(settings.services, "permissions_helper_enabled", enabled)
    monkeypatch.setattr(
        "base.host.system.probes.permissions_helper_incapability", lambda: incapability
    )


def test_screen_capture_step_records_the_helpers_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    from base.host.converge.screen_capture import (
        ScreenCaptureState,
        ScreenCaptureStatus,
        read_status,
    )

    _screen_capture_env(monkeypatch, tmp_path)
    status = ScreenCaptureStatus(
        state=ScreenCaptureState.HELPER_UNREACHABLE, diagnostic="socket did not answer"
    )
    monkeypatch.setattr(
        "services.desktop.permissions_helper.client.check_screen_capture", lambda: status
    )

    converge_host._ensure_screen_capture(_ctx(tmp_path, tmp_path))

    written = read_status()
    assert written is not None
    assert written.state is ScreenCaptureState.HELPER_UNREACHABLE
    assert "socket did not answer" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_screen_capture_step_clears_a_stale_file_when_the_grant_is_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from base.host.converge.screen_capture import (
        ScreenCaptureState,
        ScreenCaptureStatus,
        read_status,
        write_status,
    )

    _screen_capture_env(monkeypatch, tmp_path)
    write_status(ScreenCaptureStatus(state=ScreenCaptureState.NO_GRANT, diagnostic="stale"))
    monkeypatch.setattr(
        "services.desktop.permissions_helper.client.check_screen_capture",
        lambda: ScreenCaptureStatus(state=ScreenCaptureState.AVAILABLE),
    )

    converge_host._ensure_screen_capture(_ctx(tmp_path, tmp_path))
    assert read_status() is None


@pytest.mark.parametrize(
    ("enabled", "incapability"),
    [(False, None), (True, "macOS only (permissions helper drives the macOS desktop)")],
    ids=["disabled", "incapable_host"],
)
def test_screen_capture_step_skips_hosts_with_no_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled, incapability
):
    """Nothing to ask when no helper can exist here -- and the helper step has
    already said so, making a second derived complaint noise rather than news."""
    from base.host.converge.screen_capture import (
        ScreenCaptureState,
        ScreenCaptureStatus,
        read_status,
        write_status,
    )

    _screen_capture_env(monkeypatch, tmp_path, enabled=enabled, incapability=incapability)  # pyright: ignore[reportUnknownArgumentType]
    write_status(ScreenCaptureStatus(state=ScreenCaptureState.NO_GRANT, diagnostic="stale"))

    def boom():
        raise AssertionError("must not probe a host that cannot run a helper")

    monkeypatch.setattr("services.desktop.permissions_helper.client.check_screen_capture", boom)

    converge_host._ensure_screen_capture(_ctx(tmp_path, tmp_path))
    assert read_status() is None


def _accessibility_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    enabled: bool = True,
    incapability: str | None = None,
) -> None:
    from base.config import settings

    monkeypatch.setattr("base.host.converge.accessibility.ava_home", lambda: tmp_path)
    monkeypatch.setattr(settings.services, "permissions_helper_enabled", enabled)
    monkeypatch.setattr(
        "base.host.system.probes.permissions_helper_incapability", lambda: incapability
    )


def test_accessibility_step_records_the_helpers_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    from base.host.converge.accessibility import (
        AccessibilityState,
        AccessibilityStatus,
        read_status,
    )

    _accessibility_env(monkeypatch, tmp_path)
    status = AccessibilityStatus(
        state=AccessibilityState.HELPER_UNREACHABLE, diagnostic="socket did not answer"
    )
    monkeypatch.setattr(
        "services.desktop.permissions_helper.client.check_accessibility", lambda: status
    )

    converge_host._ensure_accessibility(_ctx(tmp_path, tmp_path))

    written = read_status()
    assert written is not None
    assert written.state is AccessibilityState.HELPER_UNREACHABLE
    assert "socket did not answer" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_accessibility_step_clears_a_stale_file_when_the_grant_is_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from base.host.converge.accessibility import (
        AccessibilityState,
        AccessibilityStatus,
        read_status,
        write_status,
    )

    _accessibility_env(monkeypatch, tmp_path)
    write_status(AccessibilityStatus(state=AccessibilityState.NOT_GRANTED, diagnostic="stale"))
    monkeypatch.setattr(
        "services.desktop.permissions_helper.client.check_accessibility",
        lambda: AccessibilityStatus(state=AccessibilityState.GRANTED),
    )

    converge_host._ensure_accessibility(_ctx(tmp_path, tmp_path))
    assert read_status() is None


@pytest.mark.parametrize(
    ("enabled", "incapability"),
    [(False, None), (True, "macOS only (permissions helper drives the macOS desktop)")],
    ids=["disabled", "incapable_host"],
)
def test_accessibility_step_skips_hosts_with_no_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    incapability: str | None,
):
    from base.host.converge.accessibility import (
        AccessibilityState,
        AccessibilityStatus,
        read_status,
        write_status,
    )

    _accessibility_env(monkeypatch, tmp_path, enabled=enabled, incapability=incapability)  # pyright: ignore[reportUnknownArgumentType]
    write_status(AccessibilityStatus(state=AccessibilityState.NOT_GRANTED, diagnostic="stale"))

    def boom():
        raise AssertionError("must not probe a host that cannot run a helper")

    monkeypatch.setattr("services.desktop.permissions_helper.client.check_accessibility", boom)

    converge_host._ensure_accessibility(_ctx(tmp_path, tmp_path))
    assert read_status() is None


def test_accessibility_step_follows_the_screen_capture_step():
    steps = list(converge_host.CONVERGE_STEPS)
    screen_index = next(
        i for i, step in enumerate(steps) if step.name == "screen capture availability"
    )
    screen_step = steps[screen_index]
    accessibility_step = steps[screen_index + 1]

    assert accessibility_step.name == "accessibility availability"
    assert accessibility_step.apply is converge_host._ensure_accessibility
    assert accessibility_step.roles == screen_step.roles
    assert accessibility_step.requires_unit_config == screen_step.requires_unit_config


class TestWarnUntrackedMigrations:
    """The converge step that surfaces untracked migrations/ files to the operator."""

    def test_warns_and_lists_untracked_files(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "base.deploy.schema.migrations.untracked_migration_files",
            lambda: ["20260808T010000_add-foo.sql"],
        )
        converge_host._warn_untracked_migrations(_ctx(tmp_path, tmp_path))
        out = capsys.readouterr().out
        assert "untracked" in out
        assert "20260808T010000_add-foo.sql" in out
        assert "NOT" in out and "will NOT be applied" in out

    def test_silent_when_nothing_untracked(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("base.deploy.schema.migrations.untracked_migration_files", list)
        converge_host._warn_untracked_migrations(_ctx(tmp_path, tmp_path))
        assert capsys.readouterr().out == ""

    def test_registered_gateway_only(self) -> None:
        """The warning is wired into CONVERGE_STEPS with gateway-only roles: the
        gateway is the single schema writer, so only its console should carry it."""
        step = next(
            s for s in converge_host.CONVERGE_STEPS if s.name == "untracked migrations warning"
        )
        assert step.roles == frozenset({"gateway"})
