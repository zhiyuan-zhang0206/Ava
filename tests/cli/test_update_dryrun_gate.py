"""Prepare/commit gates for gateway cluster updates."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest

from cli import commands as _cli
from cli.commands import _update_dryrun as _dryrun
from cli.commands import _update_finalize as _finalize
from cli.commands import update as _up


def _stub_prepare(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the orchestration reach prepare without a live cluster or git remote."""
    monkeypatch.setattr(
        _up,
        "_rollout_preflight",
        lambda _repo, **_kw: (None, False, "target-sha"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _up,
        "_begin_update_record",
        lambda *_args, **_kw: None,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _cli,
        "_resolve_fanout_targets",
        lambda **_kw: [],  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _up,
        "_run_preflight_fetch",
        lambda *_args, **_kw: False,  # pyright: ignore[reportUnknownArgumentType]
    )


def _run_inner(*, dry_run: bool = False) -> int:
    return _up._run_gateway_orchestration_inner(
        Path("/unused"),
        origin="test-origin",
        dry_run=dry_run,
        deploy_capability={"deploy_holder": "test", "deploy_acquired_at": "now"},
    )


def test_estimate_seeds_missing_baseline_with_conservative_first_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unseen cluster persists design targets but cannot claim an observed fast window."""
    monkeypatch.setattr(_dryrun, "ava_home", lambda: tmp_path)

    assert _dryrun.estimate_maintenance_window() == pytest.approx(65.0)  # pyright: ignore[reportUnknownMemberType]
    assert _dryrun.maintenance_window_estimate_note() == "no baseline — seeded + 25s margin"

    baseline = json.loads((tmp_path / "update-baseline.json").read_text())
    assert baseline == {
        "stages": {
            "stop_the_world": [8.0],
            "local_leg": [30.0],
            "readiness": [2.0],
            "phase_b": [45.0],
        },
        "n": 0,
    }


def test_explicit_estimate_does_not_seed_baseline_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only dry-run may use targets but must not materialize baseline state."""
    monkeypatch.setattr(_dryrun, "ava_home", lambda: tmp_path)

    assert _dryrun.estimate_maintenance_window(persist_seed=False) == pytest.approx(65.0)  # pyright: ignore[reportUnknownMemberType]
    assert not (tmp_path / "update-baseline.json").exists()


def test_staging_worktree_prunes_stale_registration_before_add(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prior interrupted dry run must not make its staging path permanently unusable."""
    commands: list[list[str]] = []

    def _run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(_dryrun, "run_bounded", _run)

    with _dryrun._staging_worktree(tmp_path, "target-sha", tmp_path / "stage"):
        pass

    assert commands[:2] == [
        ["git", "worktree", "prune"],
        ["git", "worktree", "add", "--detach", str(tmp_path / "stage"), "target-sha"],
    ]


def test_estimate_uses_stage_p95_and_baseline_writeback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit estimate is the p95 sum of the maintenance-window stages."""
    monkeypatch.setattr(_dryrun, "ava_home", lambda: tmp_path)
    (tmp_path / "update-baseline.json").write_text(
        json.dumps(
            {
                "stages": {
                    "stop_the_world": [4.0, 8.0],
                    "local_leg": [20.0, 40.0],
                    "readiness": [1.0, 2.0],
                    "phase_b": [30.0, 45.0],
                },
                "n": 2,
            }
        )
    )

    assert _dryrun.estimate_maintenance_window() == pytest.approx(50.0)  # pyright: ignore[reportUnknownMemberType]
    _dryrun.append_maintenance_baseline(
        {
            "stop_the_world": 6.0,
            "local_leg": 25.0,
            "readiness": 1.5,
            "phase_b": 35.0,
            "snapshot": 999.0,
        }
    )

    baseline = json.loads((tmp_path / "update-baseline.json").read_text())
    assert baseline["n"] == 3
    assert baseline["stages"]["stop_the_world"] == [4.0, 8.0, 6.0]
    assert "snapshot" not in baseline["stages"]


def test_phase_b_excluded_from_window_estimate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slow remote-runner convergence remains visible without enlarging maintenance."""
    monkeypatch.setattr(_dryrun, "ava_home", lambda: tmp_path)
    (tmp_path / "update-baseline.json").write_text(
        json.dumps(
            {
                "stages": {
                    "stop_the_world": [1.0, 2.0],
                    "local_leg": [3.0, 4.0],
                    "readiness": [4.0, 4.0],
                    "phase_b": [400.0, 500.0],
                },
                "n": 2,
            }
        )
    )

    assert _dryrun.estimate_maintenance_window() == pytest.approx(10.0)  # pyright: ignore[reportUnknownMemberType]
    assert _dryrun.maintenance_window_breakdown()["phase_b"] == pytest.approx(500.0)  # pyright: ignore[reportUnknownMemberType]


def test_partial_rollout_keeps_seed_values_for_unmeasured_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An abort before readiness must leave later-stage estimates usable."""
    monkeypatch.setattr(_dryrun, "ava_home", lambda: tmp_path)

    _dryrun.append_maintenance_baseline({"stop_the_world": 7.0})

    baseline = json.loads((tmp_path / "update-baseline.json").read_text())
    assert baseline["stages"] == {
        "stop_the_world": [7.0],
        "local_leg": [30.0],
        "readiness": [2.0],
        "phase_b": [45.0],
    }


def test_dry_run_checks_reports_blocking_checks_but_not_offsite_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Store reachability informs timing only; runner/build/config/import failures block commit."""
    staged: list[Path] = []

    @contextmanager
    def _staging(_repo: Path, _target_sha: str, staging_dir: Path):
        staged.append(staging_dir)
        yield staging_dir

    monkeypatch.setattr(_dryrun, "_staging_worktree", _staging)
    monkeypatch.setattr(
        _dryrun, "_runner_reachability_failures", lambda: ["runner wsl unavailable"]
    )
    monkeypatch.setattr(_dryrun, "_probe_offsite_store", lambda: "offsite store unavailable")
    monkeypatch.setattr(
        _dryrun,
        "_warm_staging_uv",
        lambda _staging: "uv sync failed",  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _dryrun,
        "_validate_target_settings",
        lambda _staging: "Settings invalid",  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _dryrun,
        "_import_candidate_modules",
        lambda _staging: ["gateway import failed"],  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _dryrun.dry_run_checks(Path("/repo"), "target-sha", staging_dir=tmp_path / "stage") == [
        "runner wsl unavailable",
        "uv sync failed",
        "Settings invalid",
        "gateway import failed",
    ]
    assert staged == [tmp_path / "stage"]


def test_offsite_probe_performs_a_read_only_remote_stat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The optional offsite signal observes a unique absent object without publishing."""
    names: list[str] = []

    class _Store:
        def stat(self, object_name: str) -> None:
            names.append(object_name)

    class _Group:
        @staticmethod
        def restartable_streaming_object_store() -> None:
            return None

        @staticmethod
        def object_store() -> _Store:
            return _Store()

    monkeypatch.setattr("services.pitr.store_factory.get_store_group", _Group)

    message = _dryrun._probe_offsite_store()
    assert message is not None
    assert message.startswith("offsite store probe ready")
    assert names and names[0].startswith("ava-logical/probe/")


def test_excessive_estimate_refuses_before_stop_or_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Changing the gate threshold branch to enter commit must fail this zero-state test."""
    _stub_prepare(monkeypatch)
    stopped: list[bool] = []
    pins: list[str] = []
    monkeypatch.setattr(_up, "dry_run_checks", lambda *_args, **_kw: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "estimate_maintenance_window", lambda: 130.0)
    monkeypatch.setattr(_up, "_snapshot_known_good", lambda **_kw: ("old", set[str](), None))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_stop_the_world", lambda *_args, **_kw: stopped.append(True))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_persist_cluster_pin", lambda sha, **_kw: pins.append(sha))  # pyright: ignore[reportUnknownArgumentType]

    assert _run_inner() == 1
    assert stopped == []
    assert pins == []


def test_gate_refusal_resumes_nothing_and_skips_local_unpause(
    monkeypatch: pytest.MonkeyPatch, local_unpauses: list[bool]
) -> None:
    """A prepare refusal cannot compensate for a Phase A pause that never began."""
    _stub_prepare(monkeypatch)
    finalized: list[tuple[list[tuple[str, str | None]], object]] = []
    tree_unpauses: list[Path] = []
    monkeypatch.setattr(
        _cli,
        "_resolve_fanout_targets",
        lambda **_kw: [("runner-a", "http://runner-a")],  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_up, "dry_run_checks", lambda *_args, **_kw: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "estimate_maintenance_window", lambda: 130.0)
    monkeypatch.setattr(
        _up,
        "_snapshot_known_good",
        lambda **_kw: ("old", set[str](), None),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _up,
        "finalize_rollout",
        lambda hosts, *_args, **kwargs: finalized.append((hosts, kwargs["outcome"])),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_finalize, "_unpause_local_via_tree", tree_unpauses.append)

    assert _run_inner() == 1
    assert finalized == [([], _up.RolloutOutcome.ABORTED)]
    assert local_unpauses == []
    assert tree_unpauses == []


def test_phase_a_started_runs_tree_unpause(monkeypatch: pytest.MonkeyPatch) -> None:
    """A post-pause abort delegates local compensation to the deployed tree."""
    _stub_prepare(monkeypatch)
    tree_unpauses: list[Path] = []
    monkeypatch.setattr(_up, "dry_run_checks", lambda *_args, **_kw: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "estimate_maintenance_window", lambda: 80.0)
    monkeypatch.setattr(
        _up,
        "_snapshot_known_good",
        lambda **_kw: ("old", set[str](), None),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _up,
        "_stop_the_world",
        lambda *_args, **_kw: (set[str](), True),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _cli,
        "_run_gateway_local_update",
        lambda *_args, **_kw: 2,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_up, "finalize_rollout", lambda *_args, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_finalize, "_unpause_local_via_tree", tree_unpauses.append)

    assert _run_inner() == 2
    assert tree_unpauses == [Path("/unused")]


def test_dry_run_failure_refuses_before_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed prepare check must never pause, stop, or begin the local leg."""
    _stub_prepare(monkeypatch)
    stopped: list[bool] = []
    monkeypatch.setattr(_up, "dry_run_checks", lambda *_args, **_kw: ["candidate import failed"])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "estimate_maintenance_window", lambda **_kw: 80.0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_snapshot_known_good", lambda **_kw: ("old", set[str](), None))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_stop_the_world", lambda *_args, **_kw: stopped.append(True))  # pyright: ignore[reportUnknownArgumentType]

    assert _run_inner() == 1
    assert stopped == []


@pytest.mark.parametrize("failure_site", ["snapshot", "checks", "estimate"])
def test_normal_prepare_error_finalizes_an_aborted_record_without_traceback(
    failure_site: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A prepare exception closes the record just like an aborted commit path."""
    _stub_prepare(monkeypatch)
    finalized: list[object] = []
    monkeypatch.setattr(
        _up,
        "_snapshot_known_good",
        lambda **_kwargs: (
            (_ for _ in ()).throw(RuntimeError("snapshot unavailable"))
            if failure_site == "snapshot"
            else ("old", set[str](), None)
        ),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _up,
        "dry_run_checks",
        lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(RuntimeError("checks unavailable"))
            if failure_site == "checks"
            else []
        ),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _up,
        "estimate_maintenance_window",
        lambda: (
            (_ for _ in ()).throw(RuntimeError("estimate unavailable"))
            if failure_site == "estimate"
            else 80.0
        ),
    )
    monkeypatch.setattr(_up, "refresh_data_plane_settings", lambda: None)
    monkeypatch.setattr("ops.cluster.unpause_local_cluster", lambda: None)
    monkeypatch.setattr("ops.cluster_pause.finalize_pause_owner_journal", lambda: None)
    monkeypatch.setattr(
        _up,
        "finalize_rollout",
        lambda *_args, **kwargs: finalized.append(kwargs["outcome"]),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _run_inner() == 1
    captured = capsys.readouterr()
    assert "prepare failed" in captured.err
    assert "Traceback" not in captured.err
    assert finalized == [_up.RolloutOutcome.ABORTED]


def test_failed_or_incomplete_commit_does_not_record_a_clean_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a fully clean commit contributes an observed maintenance baseline."""
    _stub_prepare(monkeypatch)
    baseline_calls: list[object] = []
    monkeypatch.setattr(_up, "dry_run_checks", lambda *_args, **_kwargs: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "estimate_maintenance_window", lambda: 80.0)
    monkeypatch.setattr(
        _up,
        "_snapshot_known_good",
        lambda **_kwargs: ("old", set[str](), None),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_cli, "_run_gateway_local_update", lambda *_args, **_kwargs: 2)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "refresh_data_plane_settings", lambda: None)
    monkeypatch.setattr("ops.cluster.unpause_local_cluster", lambda: None)
    monkeypatch.setattr("ops.cluster_pause.finalize_pause_owner_journal", lambda: None)
    monkeypatch.setattr(_up, "finalize_rollout", lambda *_args, **_kwargs: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _up,
        "_finalize_commit_telemetry",
        baseline_calls.append,  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _run_inner() == 2
    assert baseline_calls == []


def test_incomplete_commit_does_not_record_a_clean_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful gateway leg without readiness is still not observed clean telemetry."""
    _stub_prepare(monkeypatch)
    baseline_calls: list[object] = []
    monkeypatch.setattr(
        _cli,
        "_resolve_fanout_targets",
        lambda **_kwargs: [("runner-a", "http://runner-a")],  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_up, "dry_run_checks", lambda *_args, **_kwargs: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "estimate_maintenance_window", lambda: 80.0)
    monkeypatch.setattr(
        _up,
        "_snapshot_known_good",
        lambda **_kwargs: ("old", set[str](), None),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _up,
        "_stop_the_world",
        lambda *_args, **_kwargs: (set[str](), True),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_cli, "_run_gateway_local_update", lambda *_args, **_kwargs: 0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "refresh_data_plane_settings", lambda: None)
    monkeypatch.setattr(_up, "_persist_cluster_pin", lambda *_args, **_kwargs: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "_gateway_ready_or_incomplete", lambda *_args, **_kwargs: False)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("ops.cluster.unpause_local_cluster", lambda: None)
    monkeypatch.setattr("ops.cluster_pause.finalize_pause_owner_journal", lambda: None)
    monkeypatch.setattr(_up, "finalize_rollout", lambda *_args, **_kwargs: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _up,
        "_finalize_commit_telemetry",
        baseline_calls.append,  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _run_inner() == 1
    assert baseline_calls == []


def test_commit_clears_reconciled_markers_only_after_prepare_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconciliation is read-only until its candidate set has passed the prepare gate."""
    _stub_prepare(monkeypatch)
    order: list[str] = []
    monkeypatch.setattr(
        _cli,
        "_resolve_fanout_targets",
        lambda **_kwargs: [("runner-a", "http://runner-a")],  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_up, "dry_run_checks", lambda *_args, **_kwargs: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "estimate_maintenance_window", lambda: 80.0)
    monkeypatch.setattr(
        _up,
        "_snapshot_known_good",
        lambda **_kwargs: ("old", set[str](), None),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _up,
        "_clear_stale_stop_marker",
        lambda name: order.append(f"clear:{name}"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _up,
        "_stop_the_world",
        lambda *_args, **_kwargs: order.append("stop") or (set[str](), True),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_cli, "_run_gateway_local_update", lambda *_args, **_kwargs: 2)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "refresh_data_plane_settings", lambda: None)
    monkeypatch.setattr("ops.cluster.unpause_local_cluster", lambda: None)
    monkeypatch.setattr("ops.cluster_pause.finalize_pause_owner_journal", lambda: None)
    monkeypatch.setattr(_up, "finalize_rollout", lambda *_args, **_kwargs: None)  # pyright: ignore[reportUnknownArgumentType]

    assert _run_inner() == 2
    assert order == ["clear:runner-a", "stop"]


def test_normal_prepare_snapshots_before_maintenance_and_threads_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local leg receives the prepare result and never recreates it after Phase A."""
    _stub_prepare(monkeypatch)
    order: list[str] = []
    pull_recover = ("old-sha", {"baseline"}, Path("/backups/snapshot.dump.enc"))
    captured: dict[str, object] = {}
    monkeypatch.setattr(_up, "dry_run_checks", lambda *_args, **_kw: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "estimate_maintenance_window", lambda: 80.0)
    monkeypatch.setattr(
        _up,
        "_snapshot_known_good",
        lambda **_kw: order.append("snapshot") or pull_recover,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _up,
        "_stop_the_world",
        lambda *_args, **_kw: order.append("stop") or (set[str](), True),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _cli,
        "_run_gateway_local_update",
        lambda _repo, **kw: captured.update(kw) or 0,  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_up, "refresh_data_plane_settings", lambda: None)
    monkeypatch.setattr(_up, "_persist_cluster_pin", lambda *_args, **_kw: None)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr("ops.cluster.unpause_local_cluster", lambda: None)
    monkeypatch.setattr("ops.cluster_pause.finalize_pause_owner_journal", lambda: None)
    monkeypatch.setattr(_up, "finalize_rollout", lambda *_args, **_kw: order.append("finalize"))  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _up,
        "_finalize_commit_telemetry",
        lambda _telemetry: order.append("baseline"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _up,
        "_spawn_async_offsite_upload",
        lambda _repo, dump: order.append(f"upload:{dump}"),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _run_inner() == 0
    assert order == ["snapshot", "stop", "finalize", "baseline", f"upload:{pull_recover[2]}"]
    assert captured["pull_recover"] == pull_recover


def test_async_offsite_upload_uses_detached_module_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The local recovery artifact is retained while publication runs after commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    artifact = tmp_path / "snapshot.dump.enc"
    artifact.write_bytes(b"snapshot")
    captured: dict[str, object] = {}

    class _Popen:
        def __init__(self, argv: list[str], **kwargs: object) -> None:
            captured["argv"] = argv
            captured.update(kwargs)

    monkeypatch.setattr(_dryrun, "ava_home", lambda: tmp_path / "home")
    monkeypatch.setattr(_dryrun.subprocess, "Popen", _Popen)

    _dryrun.spawn_async_offsite_upload(repo, artifact)

    assert captured["argv"] == [
        _dryrun.sys.executable,
        "-m",
        "services.backup",
        "--publish-offsite",
        str(artifact),
    ]
    assert captured["cwd"] == repo
    assert captured["start_new_session"] is True
    assert captured["stderr"] == _dryrun.subprocess.STDOUT
    assert (tmp_path / "home" / "backups" / "db" / "upload-snapshot.dump.enc.log").exists()


def test_explicit_dry_run_never_snapshots_or_enters_maintenance(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The public dry-run path only reports phase-0 checks and the estimate verdict."""
    _stub_prepare(monkeypatch)
    preflight_options: dict[str, object] = {}
    monkeypatch.setattr(
        _up,
        "_rollout_preflight",
        lambda _repo, **kwargs: preflight_options.update(kwargs) or (None, False, "target-sha"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(_up, "dry_run_checks", lambda *_args, **_kw: [])  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(_up, "estimate_maintenance_window", lambda **_kw: 80.0)  # pyright: ignore[reportUnknownArgumentType]
    monkeypatch.setattr(
        _up,
        "maintenance_window_estimate_note",
        lambda **_kw: "no baseline — seeded + 25s margin",  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _up,
        "_snapshot_known_good",
        lambda **_kw: pytest.fail("dry-run must not create a snapshot"),  # pyright: ignore[reportUnknownArgumentType]
    )
    monkeypatch.setattr(
        _up,
        "_stop_the_world",
        lambda *_args, **_kw: pytest.fail("dry-run must not enter maintenance"),  # pyright: ignore[reportUnknownArgumentType]
    )

    assert _run_inner(dry_run=True) == 0
    assert preflight_options["prepare_only"] is True
    output = capsys.readouterr().out
    assert "PASS" in output
    assert "no baseline — seeded + 25s margin" in output


def test_cmd_update_posts_the_dry_run_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gateway receives dry-run intent instead of a client-side maintenance action."""
    from cli.commands import _update_dispatch as _dispatch

    request: dict[str, object] = {}

    class _Response:
        status_code = 202

        @staticmethod
        def json() -> dict[str, str]:
            return {"session": "ava-rollout", "log": "rollout.log"}

        @staticmethod
        def raise_for_status() -> None:
            return None

    def _post(_url: str, **kwargs: object) -> _Response:
        request.update(cast(Mapping[str, object], kwargs["json"]))
        return _Response()

    monkeypatch.setattr("shared.http_dial.post", _post)
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gateway")
    monkeypatch.setattr("shared.machine.gateway_auth_headers", dict)
    monkeypatch.setattr("shared.machine.machine_name", lambda: "test-machine")

    assert _dispatch.cmd_update(dry_run=True) == 0
    assert request["dry_run"] is True
    assert "dry-run dispatched — PASS/FAIL see rollout log" in capsys.readouterr().out
