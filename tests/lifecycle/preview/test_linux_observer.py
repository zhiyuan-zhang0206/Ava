"""Native preview evidence must retain failures and reject incomplete closure."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from scripts.preview import linux_observer as observer


def test_failed_observation_retains_evidence_without_continuing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def wrong_context(_run: Path) -> None:
        raise RuntimeError("another home")

    def unexpected_read(*_args: object) -> None:
        pytest.fail("read after refusal")

    monkeypatch.setattr(observer, "_require_context", wrong_context)
    monkeypatch.setattr(observer, "_base_observations", unexpected_read)
    with pytest.raises(RuntimeError, match="another home"):
        observer.observe(tmp_path, "failed", "running")
    result = json.loads((tmp_path / "cycle-failed.json").read_text())
    assert result["result"] == "failed"
    assert result["mode"] == "running"
    assert "another home" in result["error"]
    assert "root_status" not in result


@pytest.mark.parametrize(
    ("sampled", "completed", "expected", "verdict"),
    [
        (99, 105, True, "alive"),
        (101, 100, True, "alive"),
        (101, 105, False, "alive"),
        (101, 105, True, "unavailable"),
    ],
)
def test_redis_health_requires_current_expected_completed_round(
    sampled: int, completed: int, expected: bool, verdict: str
) -> None:
    diagnostic = {"sampled_at": sampled, "expected": expected, "last_verdict": verdict}
    body = {
        "health": {
            "diagnostic:redis-acl": diagnostic,
            "observer:root-health": {"last_completed_at": completed},
        }
    }
    result: observer.Report = {}
    with pytest.raises(RuntimeError):
        observer._observe_health(body, {"birth": 100}, result)
    assert result["redis_acl_diagnostic"] == diagnostic


def test_completed_redis_round_is_recorded() -> None:
    diagnostic = {"sampled_at": 101, "expected": True, "last_verdict": "alive"}
    result: observer.Report = {}
    observer._observe_health(
        {
            "health": {
                "diagnostic:redis-acl": diagnostic,
                "observer:root-health": {"last_completed_at": 102},
            }
        },
        {"birth": 100},
        result,
    )
    assert result["redis_acl_diagnostic"] == diagnostic


def test_manager_stop_rejects_restarted_data_plane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = {"postgres": {"pid": 41, "birth": 100.0, "starttime": 500}}
    current = {"postgres": {"pid": 41, "birth": 110.0, "starttime": 900}}

    def births(*_args: object) -> observer.Births:
        return current

    def agents(*_args: object) -> list[int]:
        return [27]

    (tmp_path / "cycle-manager-initial.json").write_text(json.dumps({"data_births": previous}))
    monkeypatch.setattr(observer, "_data_births", births)
    monkeypatch.setattr(observer, "_stored_agents", agents)
    result: observer.Report = {
        "manager": {"MainPID": "0", "ActiveState": "inactive"},
        "listeners": {},
    }
    with pytest.raises(RuntimeError, match="replaced a native data-plane birth"):
        observer._manager_stopped(tmp_path, {}, result)
    assert result["data_births"] == current
    assert result["stored_agents"] == [27]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX native flock observation")
def test_root_lock_observation_never_unlinks_a_busy_lock(tmp_path: Path) -> None:
    import fcntl

    path = tmp_path / "ava-root.lock"
    path.write_bytes(b"retained lock metadata")
    with path.open("rb") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            observer._root_lock_free(path)
    assert path.read_bytes() == b"retained lock metadata"
    assert observer._root_lock_free(path)


def test_destroy_observation_rejects_a_dangling_checkout_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / ".ava_home").symlink_to(tmp_path / "missing")

    def closed(*_args: object) -> None:
        pass

    def no_reports(*_args: object) -> list[tuple[Path, observer.Report]]:
        return []

    monkeypatch.setattr(observer, "_closed_apps", closed)
    monkeypatch.setattr(observer, "_prior_reports", no_reports)
    result: observer.Report = {
        "owned_processes": [],
        "listeners": {},
        "registry_contains_home": False,
        "unit_exists": False,
        "manager": {"LoadState": "not-found"},
    }
    with pytest.raises(RuntimeError, match="checkout binding"):
        observer._observe_stopped(tmp_path, "destroyed", {}, result)


@pytest.mark.parametrize("url", ["redis://foreign.invalid:6380", "redis://127.0.0.1:9999"])
def test_storage_observer_refuses_outside_the_reserved_endpoint(url: str) -> None:
    with pytest.raises(RuntimeError, match="reserved loopback port"):
        observer._require_private_endpoint(url, 6380)


@pytest.mark.parametrize("foreign", [False, True])
@pytest.mark.parametrize("clock_shift", [0, 3600])
def test_every_listener_must_belong_to_the_captured_application(
    monkeypatch: pytest.MonkeyPatch, foreign: bool, clock_shift: int
) -> None:
    from shared import port_preflight

    owner = observer.OwnedProcess(40, 100.0, 400)
    child = observer.OwnedProcess(41, 100.0, 401)
    outsider = observer.OwnedProcess(42, 100.0, 402)
    listeners = [child, outsider] if foreign else [child]

    def tree(_owner: observer.OwnedProcess) -> set[observer.OwnedProcess]:
        return {replace(item, birth=item.birth + clock_shift) for item in (owner, child)}

    def live(_identity: observer.OwnedProcess) -> bool:
        return True

    def native_listeners(_port: int) -> list[int]:
        return [identity.pid for identity in listeners]

    monkeypatch.setattr(observer, "capture_tree", tree)
    monkeypatch.setattr(observer.OwnedProcess, "live", live)
    monkeypatch.setattr(port_preflight, "strict_listeners_on", native_listeners)
    if foreign:
        with pytest.raises(RuntimeError, match="outside its application tree"):
            observer._require_owned_listeners(owner, listeners, 18000)
    else:
        observer._require_owned_listeners(owner, listeners, 18000)


@pytest.mark.parametrize("orphan", [False, True])
@pytest.mark.parametrize("clock_shift", [0, 3600])
def test_manager_stop_allows_only_verified_data_plane_descendants(
    monkeypatch: pytest.MonkeyPatch, orphan: bool, clock_shift: int
) -> None:
    data = observer.OwnedProcess(40, 100.0, 400)
    child = observer.OwnedProcess(41, 100.0, 401)
    outsider = observer.OwnedProcess(42, 100.0, 402)

    def tree(_owner: observer.OwnedProcess) -> set[observer.OwnedProcess]:
        return {replace(item, birth=item.birth + clock_shift) for item in (data, child)}

    def live(_identity: observer.OwnedProcess) -> bool:
        return True

    monkeypatch.setattr(observer, "capture_tree", tree)
    monkeypatch.setattr(observer.OwnedProcess, "live", live)
    result: observer.Report = {
        "data_births": {"postgres": asdict(data)},
        "owned_processes": [{"identity": asdict(outsider if orphan else child), "name": "child"}],
    }
    if orphan:
        with pytest.raises(RuntimeError, match="outside verified data/terminal custody"):
            observer._require_only_resource_survivors(result, set())
        assert result["unexpected_survivors"] == result["owned_processes"]
    else:
        observer._require_only_resource_survivors(result, set())
        assert result["unexpected_survivors"] == []


def test_manager_stop_allows_verified_terminal_tree_beside_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell = observer.OwnedProcess(41, 100.0, 401)

    def live(_identity: observer.OwnedProcess) -> bool:
        return True

    monkeypatch.setattr(observer.OwnedProcess, "live", live)
    result: observer.Report = {
        "data_births": {},
        "owned_processes": [{"identity": asdict(shell), "name": "bash"}],
    }
    observer._require_only_resource_survivors(result, {shell})
    assert result["unexpected_survivors"] == []


def test_full_stop_rejects_captured_terminal_without_record_or_private_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.preview.linux_terminals import Evidence

    shell = observer.OwnedProcess(41, 100.0, 401)
    prior: Evidence = {
        "terminals": {
            "fixture": {
                "host": asdict(shell),
                "shell": asdict(shell),
                "descendants": [],
            }
        }
    }

    def closed(*_args: object) -> None:
        pass

    def reports(_run: Path) -> list[tuple[Path, observer.Report]]:
        return [(tmp_path / "prior.json", prior)]

    def live(_identity: observer.OwnedProcess) -> bool:
        return True

    monkeypatch.setattr(observer, "_closed_apps", closed)
    monkeypatch.setattr(observer, "_prior_reports", reports)
    monkeypatch.setattr(observer.OwnedProcess, "live", live)
    result: observer.Report = {"owned_processes": [], "listeners": {}}
    with pytest.raises(RuntimeError, match="captured terminal births remain"):
        observer._observe_stopped(tmp_path, "destroyed", {}, result)
    assert result["retained_terminal_births"] == [asdict(shell)]
