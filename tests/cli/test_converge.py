from __future__ import annotations

import inspect
import json
import os
import stat
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import cast

import pytest

from cli.commands import _converge, _update_uv_sync
from cli.commands import _converge_frontend_env as _fe_env
from shared import editable_install
from shared.deploy_timing import UV_SYNC_TIMEOUT_S


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    return tmp_path


def _ctx(repo: Path, ava_home: Path, roles=None):
    return _converge.ConvergeCtx(repo=repo, ava_home=ava_home, roles=roles)  # pyright: ignore[reportUnknownArgumentType]


def test_ensure_ava_symlink_creates_and_is_idempotent(home, tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "ava").write_text("#!/bin/sh\n")
    ctx = _ctx(repo, home)  # pyright: ignore[reportUnknownArgumentType]

    _converge._ensure_ava_symlink(ctx)
    link = home / ".local" / "bin" / "ava"
    assert link.is_symlink()  # pyright: ignore[reportUnknownMemberType]
    assert link.readlink() == repo / ".venv" / "bin" / "ava"  # pyright: ignore[reportUnknownMemberType]

    _converge._ensure_ava_symlink(ctx)  # second run must not raise
    assert link.readlink() == repo / ".venv" / "bin" / "ava"  # pyright: ignore[reportUnknownMemberType]


def test_ensure_ava_symlink_repoints_stale_link(home, tmp_path: Path):
    link = home / ".local" / "bin" / "ava"
    link.parent.mkdir(parents=True)  # pyright: ignore[reportUnknownMemberType]
    link.symlink_to(  # pyright: ignore[reportUnknownMemberType]
        tmp_path / "old" / ".venv" / "bin" / "ava"
    )  # stale target  # pyright: ignore[reportUnknownMemberType]

    repo = tmp_path / "repo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "ava").write_text("#!/bin/sh\n")

    _converge._ensure_ava_symlink(_ctx(repo, home))  # pyright: ignore[reportUnknownArgumentType]

    assert link.readlink() == repo / ".venv" / "bin" / "ava"  # pyright: ignore[reportUnknownMemberType]


def test_ensure_local_bin_on_path_block_is_idempotent(home, tmp_path: Path):
    ctx = _ctx(tmp_path, home)  # pyright: ignore[reportUnknownArgumentType]
    rc = home / ".zshrc"
    rc.write_text("export FOO=1\n")  # pyright: ignore[reportUnknownMemberType]

    _converge._ensure_local_bin_on_path(ctx)
    _converge._ensure_local_bin_on_path(ctx)

    text = rc.read_text()  # pyright: ignore[reportUnknownMemberType]
    assert text.count(_converge._PATH_BEGIN) == 1  # pyright: ignore[reportUnknownMemberType]
    assert "export FOO=1" in text
    assert str(home / ".local" / "bin") in text  # pyright: ignore[reportUnknownArgumentType]


def test_ensure_ava_home_dirs(home, tmp_path: Path):
    ava_home = tmp_path / "avahome"
    _converge._ensure_ava_home_dirs(_ctx(tmp_path, ava_home))
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

    _converge._ensure_ava_home_dirs(_ctx(tmp_path, ava_home))

    for target in targets:
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(target.parent.parent.stat().st_mode) == 0o700


def test_ensure_ava_home_dirs_rejects_logs_symlink_before_writing_marker(home, tmp_path: Path):
    ava_home = tmp_path / "avahome"
    outside = tmp_path / "outside"
    outside.mkdir()
    ava_home.mkdir()
    (ava_home / "logs").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match=r"logs.*symlink"):
        _converge._ensure_ava_home_dirs(_ctx(tmp_path, ava_home))

    assert not (outside / ".metadata_never_index").exists()


def test_converge_host_runs_universal_and_skips_unit_state_when_role_none(home, tmp_path: Path):
    calls: list[str] = []
    steps = (
        _converge.ConvergeStep("wiring", lambda _: calls.append("wiring")),
        _converge.ConvergeStep("unit", lambda _: calls.append("unit"), requires_unit_config=True),
    )
    _converge.converge_host(tmp_path, None, ava_home=home, steps=steps)  # pyright: ignore[reportUnknownArgumentType]
    assert calls == ["wiring"]  # unit-state deferred when role is None


def test_converge_host_filters_by_role(home, tmp_path: Path):
    calls: list[str] = []
    steps = (
        _converge.ConvergeStep(
            "cp-only",
            lambda _: calls.append("cp"),
            roles=frozenset({"gateway"}),
        ),
        _converge.ConvergeStep("both", lambda _: calls.append("both")),
    )
    _converge.converge_host(tmp_path, frozenset({"agent-runner"}), ava_home=home, steps=steps)  # pyright: ignore[reportUnknownArgumentType]
    assert calls == ["both"]  # gateway-only step skipped on agent-runner


def test_converge_host_skips_host_global_for_dev_cluster(
    home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A dev (non-default-home) cluster must NOT run host-global wiring (the symlink /
    shell-rc edit) — those belong to the host's prod install, not a worktree."""
    monkeypatch.setattr(_converge, "is_default_home", lambda _h: False)  # pyright: ignore[reportUnknownArgumentType]
    calls: list[str] = []
    steps = (
        _converge.ConvergeStep("hostwide", lambda _: calls.append("hostwide"), host_global=True),
        _converge.ConvergeStep("percluster", lambda _: calls.append("percluster")),
    )
    _converge.converge_host(tmp_path, frozenset({"gateway"}), ava_home=home, steps=steps)  # pyright: ignore[reportUnknownArgumentType]
    assert calls == ["percluster"]  # host-global skipped


def test_converge_host_runs_host_global_for_default_cluster(
    home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The prod default home (~/.ava, non-worktree repo) DOES run host-global wiring."""
    monkeypatch.setattr(_converge, "is_default_home", lambda _h: True)  # pyright: ignore[reportUnknownArgumentType]
    calls: list[str] = []
    steps = (
        _converge.ConvergeStep("hostwide", lambda _: calls.append("hostwide"), host_global=True),
        _converge.ConvergeStep("percluster", lambda _: calls.append("percluster")),
    )
    _converge.converge_host(tmp_path, frozenset({"gateway"}), ava_home=home, steps=steps)  # pyright: ignore[reportUnknownArgumentType]
    assert calls == ["hostwide", "percluster"]


def test_prod_editable_pth_guard_is_registered_as_host_global() -> None:
    """Without host-global scoping, a dev worktree converge could rewrite its legal venv."""
    step = next(
        (step for step in _converge.CONVERGE_STEPS if step.name == "prod editable .pth target"),
        None,
    )

    assert step is not None
    assert step.host_global


@pytest.mark.skipif(os.name == "nt", reason="POSIX site-packages protection")
def test_prod_editable_dir_protection_is_host_global_and_sets_read_only_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prod converge boundary must harden site-packages after repairing records."""
    source_root = tmp_path / "prod" / "source"
    pth = source_root / ".venv" / "lib" / "python3.12" / "site-packages" / "_editable_impl_ava.pth"
    pth.parent.mkdir(parents=True)
    pth.write_text(f"{source_root}\n")
    pth.parent.chmod(0o755)
    dist_info = pth.parent / "ava-0.1.5.dist-info"
    dist_info.mkdir()
    (dist_info / "direct_url.json").write_text(
        json.dumps({"url": source_root.as_uri(), "dir_info": {"editable": True}})
    )
    dist_info.chmod(0o755)
    monkeypatch.setattr("shared.cluster_drift.prod_source_dir", lambda: source_root)
    monkeypatch.setattr("cli.commands.status._update_in_flight", lambda: False)
    step = next(
        step
        for step in _converge.CONVERGE_STEPS
        if step.name == "prod editable site-packages protection"
    )

    step.apply(_ctx(source_root, tmp_path / ".ava"))

    assert step.host_global
    assert stat.S_IMODE(pth.parent.stat().st_mode) == 0o555
    assert stat.S_IMODE(dist_info.stat().st_mode) == 0o555


@pytest.mark.skipif(os.name == "nt", reason="POSIX site-packages protection")
def test_prod_editable_dir_protection_skips_while_update_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live rollout owns writable site-packages until its syncs complete."""
    source_root = tmp_path / "prod" / "source"
    pth = source_root / ".venv" / "lib" / "python3.12" / "site-packages" / "_editable_impl_ava.pth"
    pth.parent.mkdir(parents=True)
    pth.parent.chmod(0o755)
    monkeypatch.setattr("shared.cluster_drift.prod_source_dir", lambda: source_root)
    monkeypatch.setattr("cli.commands.status._update_in_flight", lambda: True)
    step = next(
        step
        for step in _converge.CONVERGE_STEPS
        if step.name == "prod editable site-packages protection"
    )

    step.apply(_ctx(source_root, tmp_path / ".ava"))

    assert stat.S_IMODE(pth.parent.stat().st_mode) == 0o755


def test_prod_editable_pth_converge_step_repairs_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A registered but silent or non-repairing step would leave the incident invisible."""
    source_root = tmp_path / "prod" / "source"
    pth = source_root / ".venv" / "Lib" / "site-packages" / "_editable_impl_ava.pth"
    pth.parent.mkdir(parents=True)
    pth.write_text(str(tmp_path / "deleted-worktree"))
    monkeypatch.setattr("shared.cluster_drift.prod_source_dir", lambda: source_root)
    step = next(
        (step for step in _converge.CONVERGE_STEPS if step.name == "prod editable .pth target"),
        None,
    )

    if step is not None:
        step.apply(_ctx(source_root, tmp_path / ".ava"))

    assert pth.read_text() == str(source_root)
    assert "poisoned editable install" in capsys.readouterr().err


def test_prod_editable_pth_converge_step_repairs_poisoned_direct_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The converge assertion covers the direct_url record, not only the pointer."""
    source_root = tmp_path / "prod" / "source"
    du = (
        source_root
        / ".venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "ava-0.1.5.dist-info"
        / "direct_url.json"
    )
    du.parent.mkdir(parents=True)
    du.write_text(
        json.dumps(
            {"url": (tmp_path / "deleted-worktree").as_uri(), "dir_info": {"editable": True}}
        )
    )
    monkeypatch.setattr("shared.cluster_drift.prod_source_dir", lambda: source_root)
    step = next(
        (step for step in _converge.CONVERGE_STEPS if step.name == "prod editable .pth target"),
        None,
    )

    if step is not None:
        step.apply(_ctx(source_root, tmp_path / ".ava"))

    assert json.loads(du.read_text()) == {
        "url": source_root.as_uri(),
        "dir_info": {"editable": True},
    }
    assert "poisoned editable install" in capsys.readouterr().err


def test_prod_editable_pth_converge_step_leaves_healthy_direct_url_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record already naming the source root must pass the assertion untouched."""
    source_root = tmp_path / "prod" / "source"
    du = (
        source_root
        / ".venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "ava-0.1.5.dist-info"
        / "direct_url.json"
    )
    du.parent.mkdir(parents=True)
    du.write_text(json.dumps({"url": source_root.as_uri(), "dir_info": {"editable": True}}))
    original = du.read_text()
    monkeypatch.setattr("shared.cluster_drift.prod_source_dir", lambda: source_root)
    step = next(
        (step for step in _converge.CONVERGE_STEPS if step.name == "prod editable .pth target"),
        None,
    )

    if step is not None:
        step.apply(_ctx(source_root, tmp_path / ".ava"))

    assert du.read_text() == original


def _prod_editable_gate_step() -> _converge.ConvergeStep:
    return next(step for step in _converge.CONVERGE_STEPS if step.name == "prod editable exec gate")


def _write_healthy_prod_editable_install(source_root: Path) -> Path:
    site_packages = source_root / ".venv" / "lib" / "python3.12" / "site-packages"
    pth = site_packages / "_editable_impl_ava.pth"
    pth.parent.mkdir(parents=True)
    pth.write_text(f"{source_root}\n")
    direct_url = site_packages / "ava-0.1.5.dist-info" / "direct_url.json"
    direct_url.parent.mkdir()
    direct_url.write_text(json.dumps({"url": source_root.as_uri(), "dir_info": {"editable": True}}))
    launcher_dir = source_root / ".venv" / "bin"
    launcher_dir.mkdir(parents=True)
    (launcher_dir / "python").touch()
    return launcher_dir / "ava"


def test_prod_editable_exec_gate_is_host_global_and_skips_healthy_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A healthy prod venv reaches the import proof without another uv sync."""

    source_root = tmp_path / "prod" / "source"
    launcher = _write_healthy_prod_editable_install(source_root)
    launcher.touch()
    calls: list[tuple[Path, str | None]] = []

    def import_gate(
        _root: Path,
        *,
        allowed_roots: Iterable[Path] = (),
    ) -> tuple[str, ...]:
        return ()

    def sync(
        root: Path,
        *,
        timeout_s: float = UV_SYNC_TIMEOUT_S,
        reinstall_package: str | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((root, reinstall_package))
        return subprocess.CompletedProcess(["uv", "sync"], returncode=0)

    monkeypatch.setattr("shared.cluster_drift.prod_source_dir", lambda: source_root)
    monkeypatch.setattr(editable_install, "editable_import_gate", import_gate)
    monkeypatch.setattr(_update_uv_sync, "run_uv_sync", sync)

    step = _prod_editable_gate_step()
    step.apply(_ctx(source_root, tmp_path / ".ava"))

    assert step.host_global
    assert calls == []


def test_prod_editable_exec_gate_recovers_a_missing_console_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one recovery sync may restore a launcher erased by a half-uninstall."""

    source_root = tmp_path / "prod" / "source"
    launcher = _write_healthy_prod_editable_install(source_root)
    calls: list[tuple[Path, str | None]] = []
    monkeypatch.setattr("shared.cluster_drift.prod_source_dir", lambda: source_root)

    def import_gate(
        _root: Path,
        *,
        allowed_roots: Iterable[Path] = (),
    ) -> tuple[str, ...]:
        return ()

    def recover(
        root: Path,
        *,
        timeout_s: float = UV_SYNC_TIMEOUT_S,
        reinstall_package: str | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((root, reinstall_package))
        if reinstall_package == "ava":
            launcher.touch()
        return subprocess.CompletedProcess(["uv", "sync"], returncode=0)

    monkeypatch.setattr(editable_install, "editable_import_gate", import_gate)
    monkeypatch.setattr(_update_uv_sync, "run_uv_sync", recover)

    _prod_editable_gate_step().apply(_ctx(source_root, tmp_path / ".ava"))

    assert calls == [(source_root, "ava")]
    assert launcher.exists()


def test_prod_editable_exec_gate_rejects_a_fake_successful_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Converge fails when uv returns zero without restoring the missing launcher."""

    source_root = tmp_path / "prod" / "source"
    _write_healthy_prod_editable_install(source_root)
    monkeypatch.setattr("shared.cluster_drift.prod_source_dir", lambda: source_root)

    def import_gate(
        _root: Path,
        *,
        allowed_roots: Iterable[Path] = (),
    ) -> tuple[str, ...]:
        return ()

    def fake_success(
        _root: Path,
        *,
        timeout_s: float = UV_SYNC_TIMEOUT_S,
        reinstall_package: str | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(["uv", "sync"], returncode=0)

    monkeypatch.setattr(editable_install, "editable_import_gate", import_gate)
    monkeypatch.setattr(_update_uv_sync, "run_uv_sync", fake_success)

    with pytest.raises(
        RuntimeError,
        match=r"cd .* && uv sync --reinstall-package ava",
    ):
        _prod_editable_gate_step().apply(_ctx(source_root, tmp_path / ".ava"))


def test_source_tree_guard_is_registered_as_host_global() -> None:
    """Without host-global scoping, a dev worktree converge could reset its own checkout."""
    step = next(
        (step for step in _converge.CONVERGE_STEPS if step.name == "source tree reset + clean"),
        None,
    )

    assert step is not None
    assert step.host_global


def test_start_resets_source_tree_before_the_integrity_guard() -> None:
    """Task #1905 QA 2: the periodic reset must run before the start-time
    guard, or the guard's auto-heal adopts drift as the installed commit and
    the reset never bites on the regular operational path."""
    from cli.commands.start import _cmd_start_body

    src = inspect.getsource(_cmd_start_body)
    reset_at = src.index("reset_prod_source_tree(repo)")
    guard_at = src.index("_verify_source_integrity(repo)")

    assert reset_at < guard_at


def test_source_tree_step_skips_for_non_prod_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dev worktree start must never reset the prod tree (Task #1905 QA 2)."""
    source_root = tmp_path / "prod" / "source"
    source_root.mkdir(parents=True)
    dev_repo = tmp_path / "dev" / "worktree"
    dev_repo.mkdir(parents=True)
    monkeypatch.setattr("shared.cluster_drift.prod_source_dir", lambda: source_root)
    monkeypatch.setattr("cli.commands.status._update_in_flight", lambda: False)
    calls: list[object] = []

    def fake_repair(repo: object) -> None:
        calls.append(repo)

    monkeypatch.setattr("shared.source_tree_guard.repair_source_tree", fake_repair)
    step = next(
        (step for step in _converge.CONVERGE_STEPS if step.name == "source tree reset + clean"),
        None,
    )
    assert step is not None

    step.apply(_ctx(dev_repo, tmp_path / ".ava"))

    assert calls == []


def test_source_tree_step_skips_while_update_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rollout owns the tree: the reset must not fight a live checkout."""
    source_root = tmp_path / "prod" / "source"
    source_root.mkdir(parents=True)
    monkeypatch.setattr("shared.cluster_drift.prod_source_dir", lambda: source_root)
    monkeypatch.setattr("cli.commands.status._update_in_flight", lambda: True)
    calls: list[object] = []

    def fake_repair(repo: object) -> None:
        calls.append(repo)

    monkeypatch.setattr("shared.source_tree_guard.repair_source_tree", fake_repair)
    step = next(
        (step for step in _converge.CONVERGE_STEPS if step.name == "source tree reset + clean"),
        None,
    )
    assert step is not None

    step.apply(_ctx(source_root, tmp_path / ".ava"))

    assert calls == []


def test_source_tree_step_repairs_when_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Outside a rollout the step resets + cleans and says what it did."""
    source_root = tmp_path / "prod" / "source"
    source_root.mkdir(parents=True)
    monkeypatch.setattr("shared.cluster_drift.prod_source_dir", lambda: source_root)
    monkeypatch.setattr("cli.commands.status._update_in_flight", lambda: False)
    from shared.source_tree_guard import SourceTreeRepair

    def _fake_repair(_repo: object) -> SourceTreeRepair:
        return SourceTreeRepair(
            reset_from="a" * 40,
            reset_to="b" * 40,
            cleaned=("junk.txt",),
            kept_whitelisted=(),
            errors=(),
        )

    monkeypatch.setattr("shared.source_tree_guard.repair_source_tree", _fake_repair)
    step = next(
        (step for step in _converge.CONVERGE_STEPS if step.name == "source tree reset + clean"),
        None,
    )
    assert step is not None

    step.apply(_ctx(source_root, tmp_path / ".ava"))

    out = capsys.readouterr().err
    assert "source tree reset" in out
    assert "junk.txt" in out


def test_worktree_converge_does_not_touch_its_editable_pth(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worktree's own pointer is legal and must stay outside the prod-only guard."""

    def default_home(_home: Path) -> bool:
        return True

    monkeypatch.setattr(_converge, "is_default_home", default_home)
    repo = tmp_path / ".worktrees" / "feature"
    pth = repo / ".venv" / "lib" / "python3.12" / "site-packages" / "_editable_impl_ava.pth"
    pth.parent.mkdir(parents=True)
    pth.write_text(str(repo))
    step = next(
        (step for step in _converge.CONVERGE_STEPS if step.name == "prod editable .pth target"),
        None,
    )

    if step is not None:
        _converge.converge_host(
            repo,
            frozenset({"gateway"}),
            ava_home=home,
            steps=(step,),
        )

    assert pth.read_text() == str(repo)


@pytest.mark.parametrize("worktree_parent", [".claude/worktrees", ".worktrees"])
def test_converge_host_skips_host_global_in_worktree_even_if_cluster_default(
    home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worktree_parent
):
    """Fail-open guard: an uninstalled dev worktree's home resolution falls back to
    ~/.ava (the default home), but a repo under .worktrees/ or .claude/worktrees/
    is a dev worktree — host-global must still be skipped so a bare
    `ava start`/`converge` in a worktree never repoints the prod symlink."""
    monkeypatch.setattr(_converge, "is_default_home", lambda _h: True)  # pyright: ignore[reportUnknownArgumentType]
    wt_repo = tmp_path / worktree_parent / "feat-x"
    wt_repo.mkdir(parents=True)  # pyright: ignore[reportUnknownMemberType]
    calls: list[str] = []
    steps = (
        _converge.ConvergeStep("hostwide", lambda _: calls.append("hostwide"), host_global=True),
        _converge.ConvergeStep("percluster", lambda _: calls.append("percluster")),
    )
    _converge.converge_host(wt_repo, frozenset({"gateway"}), ava_home=home, steps=steps)  # pyright: ignore[reportUnknownArgumentType]
    assert calls == ["percluster"]  # host-global skipped despite cluster == default


def test_legacy_disabled_marker_step_acts_on_ctx_home(home, tmp_path: Path, capsys):
    """The step migrates the pre-rename marker in the home converge was given (not
    the settings-resolved one) and reports what it moved on stdout."""
    ava_home = tmp_path / "avahome"
    ava_home.mkdir()
    (ava_home / "skipped_services").write_text("browser\nfrontend\n")

    _converge._migrate_legacy_disabled_marker(_ctx(tmp_path, ava_home))

    assert (ava_home / "disabled_services").read_text() == "browser\nfrontend\n"
    assert "browser, frontend" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]


def test_legacy_disabled_marker_step_is_registered_and_unconditional(home, tmp_path: Path):
    """It must be in the real step list, and must not be role-scoped or deferred:
    both capabilities read the marker, and a converge on a host that is not
    configured yet still has to repair it."""
    step = next(
        s for s in _converge.CONVERGE_STEPS if s.apply is _converge._migrate_legacy_disabled_marker
    )
    assert step.roles == _converge.ALL_ROLES
    assert not step.requires_unit_config
    assert not step.host_global


def test_permissions_helper_env_key_migration_renames_legacy_keys(home, tmp_path: Path, capsys):
    """The one-shot migration renames the pre-rename AVA_NATIVE_HELPER_* keys to
    the new names in the given .env, and reports what it moved."""
    from shared.runtime_config import migrate_permissions_helper_env_keys

    ava_home = tmp_path / "avahome"
    ava_home.mkdir()
    env = ava_home / ".env"
    env.write_text(
        "AVA_CLUSTER_SECRET=sekret\nAVA_NATIVE_HELPER_ENABLED=false\nAVA_NATIVE_HELPER_PORT=18010\n"
    )

    changed = migrate_permissions_helper_env_keys(env)

    text = env.read_text()
    assert "AVA_NATIVE_HELPER_ENABLED" not in text
    assert "AVA_NATIVE_HELPER_PORT" not in text
    assert "AVA_PERMISSIONS_HELPER_ENABLED=false" in text
    assert "AVA_PERMISSIONS_HELPER_PORT=18010" in text
    assert "AVA_CLUSTER_SECRET=sekret" in text
    assert any("AVA_NATIVE_HELPER_PORT -> AVA_PERMISSIONS_HELPER_PORT" in c for c in changed)


def test_permissions_helper_env_key_migration_is_idempotent(home, tmp_path: Path):
    """A second run has no legacy keys left, so it is a no-op."""
    from shared.runtime_config import migrate_permissions_helper_env_keys

    ava_home = tmp_path / "avahome"
    ava_home.mkdir()
    env = ava_home / ".env"
    env.write_text("AVA_PERMISSIONS_HELPER_PORT=18010\n")

    assert migrate_permissions_helper_env_keys(env) == []
    assert env.read_text() == "AVA_PERMISSIONS_HELPER_PORT=18010\n"


def test_permissions_helper_env_key_migration_new_key_wins(home, tmp_path: Path):
    """Both keys present: the new key is authoritative, the legacy line is dropped."""
    from shared.runtime_config import migrate_permissions_helper_env_keys

    ava_home = tmp_path / "avahome"
    ava_home.mkdir()
    env = ava_home / ".env"
    env.write_text("AVA_NATIVE_HELPER_PORT=11111\nAVA_PERMISSIONS_HELPER_PORT=18010\n")

    changed = migrate_permissions_helper_env_keys(env)

    text = env.read_text()
    assert "AVA_NATIVE_HELPER_PORT" not in text
    assert "AVA_PERMISSIONS_HELPER_PORT=18010" in text
    assert any("dropped" in c for c in changed)


def test_permissions_helper_step_migrates_env_before_bringup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    """The helper bring-up step renames legacy env keys first, so the .env is
    canonical before the socket/plist are derived from it."""
    from shared.config import settings

    ava_home = tmp_path / "avahome"
    ava_home.mkdir()
    env = ava_home / ".env"
    env.write_text("AVA_NATIVE_HELPER_PORT=18010\n")
    monkeypatch.setattr(
        "shared.platform_probes.permissions_helper_incapability", lambda: "not macOS"
    )
    monkeypatch.setattr(settings.services, "permissions_helper_enabled", True)

    _converge._ensure_permissions_helper(_ctx(tmp_path, ava_home))

    assert "AVA_NATIVE_HELPER_PORT" not in env.read_text()
    assert "AVA_PERMISSIONS_HELPER_PORT=18010" in env.read_text()
    assert "migrated" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def _capable_helper_ctx(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A host the capability probe clears, with an .env the step can migrate."""
    from shared.config import settings

    ava_home = tmp_path / "avahome"
    ava_home.mkdir()
    (ava_home / ".env").write_text("")
    monkeypatch.setattr("shared.platform_probes.permissions_helper_incapability", lambda: None)
    monkeypatch.setattr(settings.services, "permissions_helper_enabled", True)
    return _ctx(tmp_path, ava_home)


def test_permissions_helper_step_skips_when_this_process_cannot_sign(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
):
    """The signing key lives in the login keychain, which only opens for a
    process carrying a GUI session. A converge spawned by the pin self-heal has
    none, so the same capable host signs by hand and cannot under the updater.
    That is a limit of the execution context, and converge warns past it:
    `ava start` runs converge first, so aborting here stops every service with
    nothing left to bring them back."""
    from services.permissions_helper.lifecycle import PermissionsHelperSigningUnavailableError

    ctx = _capable_helper_ctx(monkeypatch, tmp_path)

    def _cannot_sign() -> None:
        raise PermissionsHelperSigningUnavailableError("the login keychain is not unlocked")

    monkeypatch.setattr("services.permissions_helper.converge", _cannot_sign)

    _converge._ensure_permissions_helper(ctx)

    assert "the login keychain is not unlocked" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_permissions_helper_step_still_aborts_on_a_real_build_defect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Only the unreachable-key case is downgraded. A capable host that fails to
    compile or load the helper is a genuine defect and still aborts converge."""
    from services.permissions_helper.lifecycle import PermissionsHelperBuildError

    ctx = _capable_helper_ctx(monkeypatch, tmp_path)

    def _boom() -> None:
        raise PermissionsHelperBuildError("swiftc failed (1): syntax error")

    monkeypatch.setattr("services.permissions_helper.converge", _boom)

    with pytest.raises(PermissionsHelperBuildError, match="swiftc failed"):
        _converge._ensure_permissions_helper(ctx)


def test_converge_host_fail_fast_reraises(home, tmp_path: Path):
    def boom(ctx):
        raise RuntimeError("nope")

    steps = (_converge.ConvergeStep("boom", boom),)  # pyright: ignore[reportUnknownArgumentType]
    with pytest.raises(RuntimeError, match="nope"):
        _converge.converge_host(tmp_path, frozenset({"gateway"}), ava_home=home, steps=steps)  # pyright: ignore[reportUnknownArgumentType]


def test_converge_host_runs_in_order(home, tmp_path: Path):
    calls: list[str] = []
    steps = (
        _converge.ConvergeStep("first", lambda _: calls.append("first")),
        _converge.ConvergeStep("second", lambda _: calls.append("second")),
    )
    _converge.converge_host(tmp_path, frozenset({"gateway"}), ava_home=home, steps=steps)  # pyright: ignore[reportUnknownArgumentType]
    assert calls == ["first", "second"]


def test_cmd_converge_unconfigured_returns_zero(
    home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import cli.commands as _ns
    from shared import runtime_binaries as rb
    from shared.config import settings

    repo = tmp_path / "repo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "ava").write_text("#!/bin/sh\n")
    archive_shim = repo / "services" / "pitr" / "archive_shim.py"
    archive_shim.parent.mkdir(parents=True)
    archive_shim.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
    # settings is an import-time singleton, so patch the attribute directly
    # (setenv("AVA_HOME") would not be re-read).
    monkeypatch.setattr(settings.general, "ava_home", home / "avahome")  # pyright: ignore[reportUnknownArgumentType]
    # A unit test must not reach Maven Central: seed the vendored Postgres tree so
    # the vendored-binaries step takes ensure_pg_binaries()'s idempotent early
    # return (the real download is covered by tests/integration/test_vendored_binaries.py).
    monkeypatch.setattr(settings.general, "cluster_registry", str(tmp_path / "clusters.json"))
    seeded_bin = rb.vendored_pg_dir() / "bin"
    seeded_bin.mkdir(parents=True)
    (seeded_bin / "initdb").write_text("#!/bin/sh\n")
    # The pgvector injection shares the step: seed its detection file too so it
    # takes the idempotent early return (the real injection is covered by
    # scripts/pgvector_runtime_smoke.py).
    seeded_ext = rb.vendored_pg_dir() / "share/postgresql/extension"
    seeded_ext.mkdir(parents=True)
    (seeded_ext / rb._PGVECTOR_SQL).write_text("-- seeded\n")
    monkeypatch.setattr(_ns, "_repo_root", lambda: repo)
    monkeypatch.setattr(_ns, "_roles_or_none", lambda: None)

    def import_gate(
        _root: Path,
        *,
        allowed_roots: Iterable[Path] = (),
    ) -> tuple[str, ...]:
        return ()

    monkeypatch.setattr(editable_install, "editable_import_gate", import_gate)
    # host-global wiring (the ava symlink) is prod-install only, so this test must
    # run as the default home, not the suite's ambient tmpfs home.
    monkeypatch.setattr(_converge, "is_default_home", lambda _h: True)  # pyright: ignore[reportUnknownArgumentType]

    rc = _converge.cmd_converge()
    assert rc == 0
    assert (home / ".local" / "bin" / "ava").is_symlink()  # pyright: ignore[reportUnknownMemberType]


def test_reap_legacy_sessions_kills_renamed_away_only(monkeypatch: pytest.MonkeyPatch):
    """The in-socket reaper kills only `ava-<renamed-away-service>` sessions
    (e.g. the retired single `watchdog`, `runner` -> `ops`); current-named
    sessions and non-service sessions are untouched. Cluster-named schemes
    (`ava-<cluster>-*`) live on the retired per-cluster socket, which this
    socket cannot see — those are swept by `ava stop`
    (cli/commands/stop.py:_sweep_legacy_cluster_sockets)."""
    from types import SimpleNamespace

    import cli.commands._repo as repo

    killed: list[str] = []
    specs = [
        SimpleNamespace(session="ops"),
        SimpleNamespace(session="gateway-watchdog"),
        SimpleNamespace(session="agent-runner-watchdog"),
    ]
    monkeypatch.setattr(repo, "build_services", lambda: specs)
    monkeypatch.setattr(repo, "session_name", lambda s: f"ava-{s}")  # pyright: ignore[reportUnknownArgumentType]

    class _FakeBackend:
        def __init__(self) -> None:
            self.sessions = [
                "ava-watchdog",  # retired single watchdog -> reap
                "ava-runner",  # renamed away (runner -> ops) -> reap
                "ava-ops",  # current -> keep
                "ava-gateway-watchdog",  # current -> keep
                "ava-agent-42",  # agent session, never a reap target
            ]

        def list_sessions(self, prefix: str = "") -> list[str]:
            return self.sessions

        def kill_session(
            self, name: str, *, graceful: bool = False, expected: bool = False, **_: object
        ) -> tuple[bool, str]:
            killed.append(name)
            return True, "forced"

    monkeypatch.setattr("shared.session_backend.get_backend", _FakeBackend)
    _converge._reap_legacy_sessions()
    assert sorted(killed) == ["ava-runner", "ava-watchdog"]


def test_migrate_registry_keys_step_reports(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path: Path
):
    """The converge step normalizes the registry to the backward-compatible
    window form (name-keyed, compat db_name backfilled) and stays quiet when the
    file is already normalized."""
    import json

    from shared import cluster as cl

    reg = tmp_path / "clusters.json"
    reg.write_text(
        json.dumps(
            {
                "t1": {
                    "name": "t1",
                    "ports": {"gateway": 18000},
                    "gateway_home": "/h/.ava-t1",
                    "created_at": "t",
                }
            }
        )
    )
    monkeypatch.setattr(cl, "registry_path", lambda: reg)
    _converge._migrate_registry_keys_step(_ctx(tmp_path, tmp_path))
    assert "normalized clusters.json" in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]
    # Stays name-keyed (a box-shared pre-cutover reader looks up by name), with
    # the compat db_name backfilled.
    on_disk = json.loads(reg.read_text())
    assert set(on_disk) == {"t1"}
    assert on_disk["t1"]["db_name"] == cl.DATA_PLANE_IDENTITY
    _converge._migrate_registry_keys_step(_ctx(tmp_path, tmp_path))
    assert "normalized" not in capsys.readouterr().out  # pyright: ignore[reportUnknownMemberType]


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
    """Wire _ensure_pgbouncer_step's deps: a default-home record (no pgbouncer key
    → derived pooler 6433 / pg 5433), settings reflecting the toggle, and an
    optional existing .env carrying the pre-cutover AVA_DB_URL."""
    from shared import cluster

    rec = cluster.ClusterRecord(
        # A deliberately-partial record (no pgbouncer slot) to exercise the derive path.
        ports=cast("cluster.ClusterPorts", {"gateway": 8000, "postgres": 5433, "redis": 6380}),
        gateway_home=str(tmp_path),
        created_at="t",
    )
    monkeypatch.setattr(cluster, "get_record", lambda _home: rec)  # pyright: ignore[reportUnknownArgumentType]
    # This record predates the pgbouncer slot; treat its home as the default home
    # so record_pgbouncer_port derives the fixed legacy 6433.
    monkeypatch.setattr(cluster, "is_default_home", lambda _h: True)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_converge.settings.data_plane, "pgbouncer_enabled", enabled)
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
    _converge._ensure_pgbouncer_step(ctx)
    env = (tmp_path / ".env").read_text()
    assert "AVA_DB_URL=" + _POOLED_URL in env  # main's derived legacy pooler 6433
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
    monkeypatch.setattr(_converge.settings.data_plane, "db_url", remote_url)
    monkeypatch.setattr(_converge.settings.data_plane, "redis_url", "rediss://10.9.8.7:6380/0")
    _converge._ensure_pgbouncer_step(ctx)
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
    _converge._ensure_pgbouncer_step(ctx)
    env = (tmp_path / ".env").read_text()
    assert "AVA_DB_URL=" + _DIRECT_URL in env
    assert "AVA_PGBOUNCER_PORT" not in env


def test_ensure_pgbouncer_step_leaves_matching_url_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A URL that already matches the toggle is not rewritten — no snapshot churn
    every start — but the retired key is still dropped."""
    ctx = _pgbouncer_ctx(tmp_path, monkeypatch, db_url=_POOLED_URL, enabled=True)
    _converge._ensure_pgbouncer_step(ctx)
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
    _converge._ensure_pgbouncer_step(ctx)
    env = (tmp_path / ".env").read_text()
    assert "AVA_DB_URL=postgresql://ava:dev@localhost:5432/ava" in env
    assert "AVA_PGBOUNCER_PORT" not in env


def test_ensure_pgbouncer_step_without_env_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """No .env (a fresh home converge runs before birth materializes URLs): the
    step is a no-op, not a crash."""
    ctx = _pgbouncer_ctx(tmp_path, monkeypatch, db_url=None, enabled=True)
    _converge._ensure_pgbouncer_step(ctx)
    assert not (tmp_path / ".env").exists()


def _rw_url(pw: str, *, host: str, user: str = "") -> str:
    """Build a credentialed redis URL from parts, so the source carries no
    `scheme://user:password@host` literal for a secret scanner to flag (same
    convention as tests/shared/test_url_secret.py) — every value is a throwaway
    fixture, not a real credential."""
    return f"redis://{user}:{pw}@{host}/0"


def _rw_pg_url(pw: str, *, host: str, user: str = "ava_main") -> str:
    """The postgresql twin of _rw_url (parts-built, scanner-safe)."""
    return f"postgresql://{user}:{pw}@{host}/ava_main"


def _redis_identity_ctx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, db_url: str, secret: str
):
    """Wire the legacy backfill's db identity and pre-split bearer fallback."""
    monkeypatch.setattr(_converge.settings.data_plane, "db_url", db_url)
    monkeypatch.setattr(_converge.settings.data_plane, "cluster_secret", secret)
    monkeypatch.setattr(
        _converge.settings.data_plane, "redis_url", _converge.settings.data_plane.redis_url
    )
    # Backfill rebuilds DataPlaneSettings, which reads the actual environment.
    monkeypatch.setitem(os.environ, "AVA_REDIS_URL", _converge.settings.data_plane.redis_url)
    return _ctx(tmp_path / "repo", tmp_path)


def test_redis_url_identity_step_backfills_missing_username(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A pre-split username-less URL gets the db_url identity and bearer fallback;
    a split home instead reads its runtime password from the file."""
    (tmp_path / ".env").write_text(
        f"AVA_REDIS_URL={_rw_url('pw', host='redis.lan:6380')}\n"
        f"AVA_DB_URL={_rw_pg_url('pw', host='db.lan:5433')}\n"
    )
    ctx = _redis_identity_ctx(
        tmp_path,
        monkeypatch,
        db_url=_rw_pg_url("sek", host="127.0.0.1:5433"),
        secret="sek",  # noqa: S106 — test fixture
    )
    monkeypatch.setitem(os.environ, "AVA_MACHINE_HOST", "redis.lan")
    _converge._ensure_redis_url_identity_step(ctx)
    env = (tmp_path / ".env").read_text()
    assert f"AVA_REDIS_URL={_rw_url('sek', host='redis.lan:6380', user='ava_main')}" in env
    # The reachable host is preserved from the FILE (never the settings dial
    # value, which is loopback-rewritten), and the db line is untouched.
    assert f"AVA_DB_URL={_rw_pg_url('pw', host='db.lan:5433')}" in env
    assert os.environ["AVA_REDIS_URL"] == _rw_url("sek", host="redis.lan:6380", user="ava_main")
    assert _converge.settings.data_plane.redis_url == _rw_url(
        "sek", host="127.0.0.1:6380", user="ava_main"
    )


def test_redis_url_identity_step_skips_when_username_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A URL already carrying a username is left byte-identical (idempotent — no
    rewrite churn on every `ava start`)."""
    original = "AVA_REDIS_URL=redis://ava:pw@127.0.0.1:6380/0\n"
    (tmp_path / ".env").write_text(original)
    ctx = _redis_identity_ctx(
        tmp_path,
        monkeypatch,
        db_url="postgresql://ava_main:s@127.0.0.1:5433/ava_main",
        secret="s",  # noqa: S106 — test fixture
    )
    _converge._ensure_redis_url_identity_step(ctx)
    assert (tmp_path / ".env").read_text() == original


def test_redis_url_identity_step_falls_back_to_birth_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """db_url carrying no username either (unanchored sentinel / legacy) falls back
    to the fixed birth identifier — the ACL user is auto-created, so any identifier
    is safe to adopt."""
    (tmp_path / ".env").write_text("AVA_REDIS_URL=redis://:pw@127.0.0.1:6380/0\n")
    ctx = _redis_identity_ctx(
        tmp_path,
        monkeypatch,
        db_url="postgresql://127.0.0.1:5433/postgres",
        secret="sek",  # noqa: S106 — test fixture
    )
    _converge._ensure_redis_url_identity_step(ctx)
    assert "AVA_REDIS_URL=redis://ava:sek@127.0.0.1:6380/0" in (tmp_path / ".env").read_text()


def test_redis_url_identity_step_backfills_nopass_without_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A no-auth legacy URL gains the named nopass identity without a credential."""
    (tmp_path / ".env").write_text("AVA_REDIS_URL=redis://:pw@127.0.0.1:6380/0\n")
    ctx = _redis_identity_ctx(
        tmp_path, monkeypatch, db_url="postgresql://ava_main:s@127.0.0.1:5433/ava_main", secret=""
    )
    _converge._ensure_redis_url_identity_step(ctx)
    assert (tmp_path / ".env").read_text() == "AVA_REDIS_URL=redis://ava_main@127.0.0.1:6380/0\n"
    assert _converge.settings.data_plane.redis_url == "redis://ava_main@127.0.0.1:6380/0"


def test_redis_url_identity_step_skips_without_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = _redis_identity_ctx(
        tmp_path,
        monkeypatch,
        db_url="postgresql://ava_main:s@127.0.0.1:5433/ava_main",
        secret="s",  # noqa: S106 — test fixture
    )
    _converge._ensure_redis_url_identity_step(ctx)
    assert not (tmp_path / ".env").exists()


# --- watchdog probe registration ------------------------------------------
# The step fans out over the unit's capability SET. A single box carries both
# capabilities and therefore runs TWO watchdog daemons; registering one probe
# there would leave one of them unsupervised — the same collision that split the
# watchdog daemon per-capability in the first place.


def _record_registrations(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr("shared.os_watchdog_probe.register_watchdog_probe", seen.append)
    return seen


def test_watchdog_probe_single_box_registers_both_capabilities(
    home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    seen = _record_registrations(monkeypatch)
    _converge.ensure_watchdog_probe(
        _ctx(tmp_path, home, roles=frozenset({"gateway", "agent-runner"}))  # pyright: ignore[reportUnknownArgumentType]
    )
    assert sorted(seen) == ["agent-runner", "gateway"]


def test_watchdog_probe_split_runner_registers_only_its_own(
    home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    seen = _record_registrations(monkeypatch)
    _converge.ensure_watchdog_probe(_ctx(tmp_path, home, roles=frozenset({"agent-runner"})))  # pyright: ignore[reportUnknownArgumentType]
    assert seen == ["agent-runner"]


def test_watchdog_probe_split_gateway_registers_only_its_own(
    home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    seen = _record_registrations(monkeypatch)
    _converge.ensure_watchdog_probe(_ctx(tmp_path, home, roles=frozenset({"gateway"})))  # pyright: ignore[reportUnknownArgumentType]
    assert seen == ["gateway"]


def test_watchdog_probe_unconfigured_unit_registers_nothing(
    home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A fresh install has no capability set yet; registering a job for a unit
    that does not know what it runs would point at services that never start."""
    seen = _record_registrations(monkeypatch)
    _converge.ensure_watchdog_probe(_ctx(tmp_path, home, roles=None))  # pyright: ignore[reportUnknownArgumentType]
    assert seen == []


def test_watchdog_probe_ignores_unknown_capability_tokens(
    home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`roles` is frozenset[str] off the DB, so an unknown token must be dropped
    rather than turned into a job for a watchdog that does not exist."""
    seen = _record_registrations(monkeypatch)
    _converge.ensure_watchdog_probe(_ctx(tmp_path, home, roles=frozenset({"gateway", "future"})))  # pyright: ignore[reportUnknownArgumentType]
    assert seen == ["gateway"]


def test_watchdog_probe_step_runs_on_both_roles():
    """Not gateway-gated: the gap was observed on an agent-runner-only box."""
    step = next(s for s in _converge.CONVERGE_STEPS if s.name == "watchdog probe job")
    assert step.roles == _converge.ALL_ROLES
    assert step.requires_unit_config is True


# --- stale schtasks reap ---------------------------------------------------
# A home-slug change leaves ghost tasks firing under the old \Ava\ folder,
# racing the current slug's /Create on every converge (win 2026-08-11, task
# #1196). The reap runs ahead of the register steps, on any serving role.


def test_reap_stale_schtasks_calls_reap(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from cli.commands import _converge_os_jobs

    seen: list[str] = []
    monkeypatch.setattr("shared.os_schtasks.reap_stale_tasks", lambda: seen.append("reaped") or 1)
    _converge_os_jobs.reap_stale_schtasks(_ctx(tmp_path, home))  # pyright: ignore[reportUnknownArgumentType]
    assert seen == ["reaped"]


def test_reap_stale_schtasks_step_runs_on_both_roles():
    """Not role-gated: any serving unit may carry a stale-slug ghost, and the
    reap is a no-op on POSIX hosts."""
    from cli.commands import _converge_os_jobs

    step = next(s for s in _converge.CONVERGE_STEPS if s.name == "reap stale Windows tasks")
    assert step.roles == _converge.ALL_ROLES
    assert step.requires_unit_config is True
    assert step.apply is _converge_os_jobs.reap_stale_schtasks


def _screen_capture_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, enabled=True, incapability=None
):
    from shared.config import settings

    monkeypatch.setattr("shared.screen_capture.ava_home", lambda: tmp_path)
    monkeypatch.setattr(settings.services, "permissions_helper_enabled", enabled)
    monkeypatch.setattr(
        "shared.platform_probes.permissions_helper_incapability", lambda: incapability
    )


def test_screen_capture_step_records_the_helpers_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    from shared.screen_capture import ScreenCaptureState, ScreenCaptureStatus, read_status

    _screen_capture_env(monkeypatch, tmp_path)
    status = ScreenCaptureStatus(
        state=ScreenCaptureState.HELPER_UNREACHABLE, diagnostic="socket did not answer"
    )
    monkeypatch.setattr("services.permissions_helper.client.check_screen_capture", lambda: status)

    _converge._ensure_screen_capture(_ctx(tmp_path, tmp_path))

    written = read_status()
    assert written is not None
    assert written.state is ScreenCaptureState.HELPER_UNREACHABLE
    assert "socket did not answer" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_screen_capture_step_clears_a_stale_file_when_the_grant_is_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from shared.screen_capture import (
        ScreenCaptureState,
        ScreenCaptureStatus,
        read_status,
        write_status,
    )

    _screen_capture_env(monkeypatch, tmp_path)
    write_status(ScreenCaptureStatus(state=ScreenCaptureState.NO_GRANT, diagnostic="stale"))
    monkeypatch.setattr(
        "services.permissions_helper.client.check_screen_capture",
        lambda: ScreenCaptureStatus(state=ScreenCaptureState.AVAILABLE),
    )

    _converge._ensure_screen_capture(_ctx(tmp_path, tmp_path))
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
    from shared.screen_capture import (
        ScreenCaptureState,
        ScreenCaptureStatus,
        read_status,
        write_status,
    )

    _screen_capture_env(monkeypatch, tmp_path, enabled=enabled, incapability=incapability)  # pyright: ignore[reportUnknownArgumentType]
    write_status(ScreenCaptureStatus(state=ScreenCaptureState.NO_GRANT, diagnostic="stale"))

    def boom():
        raise AssertionError("must not probe a host that cannot run a helper")

    monkeypatch.setattr("services.permissions_helper.client.check_screen_capture", boom)

    _converge._ensure_screen_capture(_ctx(tmp_path, tmp_path))
    assert read_status() is None


def _accessibility_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    enabled: bool = True,
    incapability: str | None = None,
) -> None:
    from shared.config import settings

    monkeypatch.setattr("shared.accessibility.ava_home", lambda: tmp_path)
    monkeypatch.setattr(settings.services, "permissions_helper_enabled", enabled)
    monkeypatch.setattr(
        "shared.platform_probes.permissions_helper_incapability", lambda: incapability
    )


def test_accessibility_step_records_the_helpers_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    from shared.accessibility import AccessibilityState, AccessibilityStatus, read_status

    _accessibility_env(monkeypatch, tmp_path)
    status = AccessibilityStatus(
        state=AccessibilityState.HELPER_UNREACHABLE, diagnostic="socket did not answer"
    )
    monkeypatch.setattr("services.permissions_helper.client.check_accessibility", lambda: status)

    _converge._ensure_accessibility(_ctx(tmp_path, tmp_path))

    written = read_status()
    assert written is not None
    assert written.state is AccessibilityState.HELPER_UNREACHABLE
    assert "socket did not answer" in capsys.readouterr().err  # pyright: ignore[reportUnknownMemberType]


def test_accessibility_step_clears_a_stale_file_when_the_grant_is_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from shared.accessibility import (
        AccessibilityState,
        AccessibilityStatus,
        read_status,
        write_status,
    )

    _accessibility_env(monkeypatch, tmp_path)
    write_status(AccessibilityStatus(state=AccessibilityState.NOT_GRANTED, diagnostic="stale"))
    monkeypatch.setattr(
        "services.permissions_helper.client.check_accessibility",
        lambda: AccessibilityStatus(state=AccessibilityState.GRANTED),
    )

    _converge._ensure_accessibility(_ctx(tmp_path, tmp_path))
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
    from shared.accessibility import (
        AccessibilityState,
        AccessibilityStatus,
        read_status,
        write_status,
    )

    _accessibility_env(monkeypatch, tmp_path, enabled=enabled, incapability=incapability)  # pyright: ignore[reportUnknownArgumentType]
    write_status(AccessibilityStatus(state=AccessibilityState.NOT_GRANTED, diagnostic="stale"))

    def boom():
        raise AssertionError("must not probe a host that cannot run a helper")

    monkeypatch.setattr("services.permissions_helper.client.check_accessibility", boom)

    _converge._ensure_accessibility(_ctx(tmp_path, tmp_path))
    assert read_status() is None


def test_accessibility_step_follows_the_screen_capture_step():
    steps = list(_converge.CONVERGE_STEPS)
    screen_index = next(
        i for i, step in enumerate(steps) if step.name == "screen capture availability"
    )
    screen_step = steps[screen_index]
    accessibility_step = steps[screen_index + 1]

    assert accessibility_step.name == "accessibility availability"
    assert accessibility_step.apply is _converge._ensure_accessibility
    assert accessibility_step.roles == screen_step.roles
    assert accessibility_step.requires_unit_config == screen_step.requires_unit_config


class TestWarnUntrackedMigrations:
    """The converge step that surfaces untracked migrations/ files to the operator."""

    def test_warns_and_lists_untracked_files(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "shared.migrations.untracked_migration_files",
            lambda: ["20260808T010000_add-foo.sql"],
        )
        _converge._warn_untracked_migrations(_ctx(tmp_path, tmp_path))
        out = capsys.readouterr().out
        assert "untracked" in out
        assert "20260808T010000_add-foo.sql" in out
        assert "NOT" in out and "will NOT be applied" in out

    def test_silent_when_nothing_untracked(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("shared.migrations.untracked_migration_files", list)
        _converge._warn_untracked_migrations(_ctx(tmp_path, tmp_path))
        assert capsys.readouterr().out == ""

    def test_registered_gateway_only(self) -> None:
        """The warning is wired into CONVERGE_STEPS with gateway-only roles: the
        gateway is the single schema writer, so only its console should carry it."""
        step = next(s for s in _converge.CONVERGE_STEPS if s.name == "untracked migrations warning")
        assert step.roles == frozenset({"gateway"})
