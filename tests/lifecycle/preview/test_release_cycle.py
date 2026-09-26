"""Image-cycle orchestration preserves failures and never retries release effects."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.preview import local, release_cycle
from tests.lifecycle.preview.test_local import repo as repo


@pytest.fixture
def cycle(repo: Path, tmp_path: Path) -> release_cycle.ReleaseCycle:
    preview = local.create(repo, "HEAD", tmp_path / "runs")
    receipts = [tmp_path / f"{name}.json" for name in ("a", "b")]
    for path in receipts:
        path.write_text(
            json.dumps({"source": {"source_commit": "a" * 40}, "request": {"commit": "a" * 40}})
        )
    return release_cycle.ReleaseCycle(
        preview, *(release_cycle.CapturedReceipt.read(p) for p in receipts)
    )


@pytest.mark.parametrize("failed", [None, "inputs", "image-a", "a-to-b", "b-to-a", "cleanup"])
def test_full_cycle_keeps_original_failure_when_cleanup_passes(
    cycle: release_cycle.ReleaseCycle, monkeypatch: pytest.MonkeyPatch, failed: str | None
) -> None:
    reached: list[str] = []

    def phase(name: str) -> dict[str, Any]:
        reached.append(name)
        if name == failed:
            raise RuntimeError(name)
        return {}

    monkeypatch.setattr(cycle, "initialized", lambda: phase("inputs"))

    def image_a(_before: dict[str, Any]) -> dict[str, Any]:
        return phase("image-a")

    def transition(label: str, *_args: object) -> dict[str, Any]:
        return phase("a-to-b" if label == "ab" else "b-to-a")

    monkeypatch.setattr(cycle, "image_a", image_a)
    monkeypatch.setattr(cycle, "transition", transition)
    monkeypatch.setattr(cycle, "cleanup", lambda: phase("cleanup"))
    if failed:
        with pytest.raises(RuntimeError, match=failed):
            cycle.run()
    else:
        cycle.run()
    proof = json.loads(cycle.path.read_text())
    assert proof["result"] == ("failed" if failed else "passed")
    assert reached[-1] == "cleanup"
    if failed:
        assert proof["phases"][failed]["result"] == "failed"
    assert all("finished_at" in row for row in proof["phases"].values())


def test_preflight_failure_leaves_initialized_source_untouched(
    cycle: release_cycle.ReleaseCycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release_cycle.sys, "platform", "linux")
    cycle.preview.data.update(state="ready", verification="passed")
    (cycle.preview.run / "smoke.json").write_text('{"agent": 7}')
    monkeypatch.setattr(cycle.preview, "assert_checkout", lambda: None)
    actions: list[str] = []

    def adapter(action: str, *_args: str) -> None:
        actions.append(action)
        raise RuntimeError("fixture not installed in image")

    monkeypatch.setattr(cycle, "adapter", adapter)
    with pytest.raises(RuntimeError, match="fixture not installed"):
        cycle.run()
    assert actions == ["prepare"]
    assert cycle.proof["effects_started"] is False
    assert cycle.proof["phases"]["cleanup"]["result"] == "passed"


@pytest.mark.parametrize("failed_wait", [False, True])
def test_transition_requires_closed_success_before_retirement_and_immediate_smoke(
    cycle: release_cycle.ReleaseCycle, monkeypatch: pytest.MonkeyPatch, *, failed_wait: bool
) -> None:
    events: list[str] = []

    def adapter(action: str, *_args: str) -> None:
        events.append(action)
        if action == "wait" and failed_wait:
            raise RuntimeError("executor failed")

    def observe(_label: str, **_kwargs: object) -> dict[str, Any]:
        events.append("observe")
        return {}

    monkeypatch.setattr(cycle, "adapter", adapter)

    def smoke(_label: str) -> None:
        events.append("smoke")

    def preserved(*_args: object) -> None:
        pass

    monkeypatch.setattr(cycle, "smoke", smoke)
    monkeypatch.setattr(cycle, "observe", observe)
    monkeypatch.setattr(cycle, "preserved", preserved)
    if failed_wait:
        with pytest.raises(RuntimeError, match="executor failed"):
            cycle.transition("ab", "b", {})
        assert events == ["submit", "wait"]
    else:
        cycle.transition("ab", "b", {})
        assert events == [
            "submit",
            "wait",
            "closed",
            "smoke",
            "submit",
            "retired",
            "observe",
            "state",
            "freeze",
            "capture",
        ]


def test_live_executor_blocks_stop_destroy_and_unknown_signals(
    cycle: release_cycle.ReleaseCycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    cycle.proof["effects_started"] = True
    events: list[str] = []

    def unsettled(action: str, *_args: str) -> None:
        assert action == "settle"
        raise TimeoutError("finite executor remains live")

    monkeypatch.setattr(cycle, "adapter", unsettled)

    def cli(name: str, _args: list[str]) -> None:
        events.append(name)

    monkeypatch.setattr(cycle, "cli", cli)

    def command(name: str, _args: list[str], **_kwargs: object) -> None:
        events.append(name)

    def observed(_label: str, **_kwargs: object) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(cycle, "command", command)
    monkeypatch.setattr(cycle, "observe", observed)
    with pytest.raises(TimeoutError, match="remains live"):
        cycle.cleanup()
    assert not events


def test_each_command_rebuilds_its_clean_environment(
    cycle: release_cycle.ReleaseCycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    cycle.preview.env["PYTHONPATH"] = "/foreign"
    observations: list[dict[str, str]] = []

    def command(_name: str, _argv: list[str], **_kwargs: object) -> None:
        observations.append(dict(cycle.preview.env))
        cycle.preview.env["PYTHONPATH"] = "/another"

    monkeypatch.setattr(cycle.preview, "command", command)
    cycle.command("one", ["inert"])
    cycle.command("two", ["inert"])
    assert observations[0] == observations[1]
    assert "PYTHONPATH" not in observations[0]
    assert observations[0]["AVA_HOME"] == str(cycle.preview.home)


@pytest.mark.parametrize(
    "changed", [None, "ports", "hashes", "data_births", "births", "service_path"]
)
def test_image_replacement_keeps_home_state_and_data_but_replaces_app_births(
    changed: str | None,
) -> None:
    before = {
        "ports": {"gateway": 5000},
        "hashes": {"home/.env": "same"},
        "data_births": {"postgres": {"pid": 2, "birth": 1, "starttime": 10}},
        "births": {"root": {"pid": 4, "birth": 1, "starttime": 11}},
        "service_path": {"declared": "/usr/bin", "root_path": "/A/bin:/usr/bin"},
    }
    after = before | {
        "births": {"root": {"pid": 5, "birth": 2, "starttime": 20}},
        "service_path": {"declared": "/usr/bin", "root_path": "/B/bin:/usr/bin"},
    }
    if changed == "births":
        after[changed] = before[changed]
    elif changed is not None:
        after[changed] = {"declared": "wrong"} if changed == "service_path" else {"wrong": 0}
    if changed:
        with pytest.raises(RuntimeError):
            release_cycle.ReleaseCycle.preserved(before, after)
    else:
        release_cycle.ReleaseCycle.preserved(before, after)


def test_prior_app_survivor_blocks_new_smoke_before_retirement(
    cycle: release_cycle.ReleaseCycle, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    def action(name: str, *_args: str) -> None:
        events.append(name)
        if name == "closed":
            raise RuntimeError("prior app generation remains live")

    def smoke(_label: str) -> None:
        pytest.fail("new workload must not precede prior app closure")

    monkeypatch.setattr(cycle, "adapter", action)
    monkeypatch.setattr(cycle, "smoke", smoke)
    with pytest.raises(RuntimeError, match="prior app"):
        cycle.transition("ab", "b", {})
    assert events == ["submit", "wait", "closed"]
