"""Start consumes the verified loaded release without a Git checkout or shell."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shlex
from argparse import Namespace
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cli import start_runtime
from cli.commands import _root_driver, _start_generation
from ops.service_spec import ServiceSpec
from services.ava_root_glue import manifests
from shared import runtime_interpreter
from shared.runtime_release import (
    ReleaseRejectedError,
    VerifiedRelease,
    file_sha256,
    verify_release,
)
from shared.start_inputs import configuration_digest, require_configuration


@pytest.fixture
def image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> VerifiedRelease:
    home = tmp_path / "home"
    root = home / "releases" / ("a" * 64)
    package = root / "venv/lib/python3.12/site-packages"
    contents = {
        "venv/bin/python": b"private interpreter",
        "venv/bin/other-python": b"different interpreter",
        "venv/lib/python3.12/site-packages/db/schema.sql": b"baseline",
        "venv/lib/python3.12/site-packages/services/ava_root/__main__.py": b"# root",
        "venv/lib/python3.12/site-packages/gateway/__main__.py": b"# gateway",
    }
    schema = hashlib.sha256(b"baseline").hexdigest()
    identity = {
        "version": 1,
        "source_commit": "b" * 40,
        "source_tree": "c" * 40,
        "source_archive_digest": "d" * 64,
        "schema_digest": schema,
        "applied_names": ["20260101T000000_example.sql"],
    }
    contents["venv/lib/python3.12/site-packages/shared/release-build.json"] = (
        json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    for name, content in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    manifest = {
        "version": 1,
        "artifact_digest": root.name,
        "platform": platform.platform(),
        "schema_digest": schema,
        "interpreter": "venv/bin/python",
        "cwd": "venv",
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()},
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    result = verify_release(
        root.parent,
        root.name,
        manifest_digest=file_sha256(root / "manifest.json"),
        platform_tag=platform.platform(),
        schema_digest=schema,
    )
    (root.parent / "current-release").write_text(
        json.dumps(
            {
                "artifact_digest": result.digest,
                "manifest_digest": result.manifest_digest,
            }
        )
    )
    (home / ".env").write_text("AVA_MACHINE_NAME=fixture\n")
    (home / "start-intent.json").write_text(
        json.dumps(
            {
                "version": 1,
                "home": str(home),
                "checkout": str(tmp_path / "original-source"),
                "worktree": False,
                "roles": ["agent-runner"],
                "config_digest": None,
                "phase": "ready",
                "record": None,
                "env": {"AVA_MACHINE_NAME": "fixture"},
            }
        )
    )

    def loaded() -> tuple[Path, Path, Path, bool]:
        return root / "venv", result.interpreter, package, True

    monkeypatch.setattr(runtime_interpreter, "loaded_runtime", loaded)
    return result


def _admit(image: VerifiedRelease) -> start_runtime.StartRuntime:
    return start_runtime.admit_release(
        image.root.parent.parent,
        image,
        schema_digest=hashlib.sha256(b"baseline").hexdigest(),
        source_commit="b" * 40,
    )


def test_migration_proof_uses_real_start_admission_before_setup(
    image: VerifiedRelease, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Source fixture tests the refusal boundary; the installed-wheel proof runs in CI."""
    from scripts.prove_runtime_migration import prove_start_barrier

    runtime = _admit(image)
    home = image.root.parent.parent
    (home / "releases/current-release").unlink()
    (home / "start-intent.json").unlink()
    monkeypatch.setattr("shared.paths.ava_home", lambda: home)
    prove_start_barrier(runtime)


def test_release_root_and_services_use_captured_isolated_direct_argv(
    image: VerifiedRelease,
) -> None:
    runtime = _admit(image)
    run = image.root.parent.parent / "run"
    assert _root_driver._root_argv(run, run / "units.json", runtime)[:7] == [
        str(image.interpreter),
        "-I",
        "-B",
        "-X",
        "utf8",
        "-m",
        "services.ava_root",
    ]
    spec = ServiceSpec(
        session="gateway",
        cmd=".venv/bin/python -m gateway",
        capabilities=frozenset({"gateway"}),
        requires_db=True,
    )
    unit = manifests.build_units(
        [spec], capabilities=["gateway"], repo_root=runtime.code_root, release=image
    )[0]
    assert unit["exec"] == list(image.module_argv("gateway"))
    assert runtime.cwd == image.cwd


@pytest.mark.parametrize("mismatch", ["executable", "prefix", "package", "isolation"])
def test_loaded_runtime_must_match_verified_interpreter_and_package(
    image: VerifiedRelease,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    actual = runtime_interpreter.loaded_runtime()
    values: list[Path | bool] = list(actual)
    values[{"prefix": 0, "executable": 1, "package": 2, "isolation": 3}[mismatch]] = (
        False if mismatch == "isolation" else Path("/outside-image")
    )

    def loaded() -> tuple[Path | bool, ...]:
        return tuple(values)

    monkeypatch.setattr(runtime_interpreter, "loaded_runtime", loaded)
    with pytest.raises(ReleaseRejectedError, match="isolated -I -B"):
        _admit(image)


def test_tampered_member_is_rejected_even_with_original_manifest(image: VerifiedRelease) -> None:
    image.interpreter.write_bytes(b"changed")
    with pytest.raises(ReleaseRejectedError, match="hash mismatch"):
        _admit(image)


def test_captured_paths_cannot_override_verified_manifest(image: VerifiedRelease) -> None:
    with pytest.raises(ReleaseRejectedError, match="captured release paths"):
        _admit(replace(image, cwd=image.root))


def test_wrong_commit_is_not_admitted(image: VerifiedRelease) -> None:
    with pytest.raises(ReleaseRejectedError, match="target commit"):
        start_runtime.admit_release(
            image.root.parent.parent,
            image,
            schema_digest=hashlib.sha256(b"baseline").hexdigest(),
            source_commit="e" * 40,
        )


def test_executor_origin_does_not_grant_start_without_selection(image: VerifiedRelease) -> None:
    selector = image.root.parent / "current-release"
    selector.unlink()
    runtime = start_runtime.StartRuntime.from_image(
        image.root.parent.parent,
        image,
        schema_digest=hashlib.sha256(b"baseline").hexdigest(),
        source_commit="b" * 40,
    )
    with pytest.raises(ReleaseRejectedError, match="selected captured image"):
        runtime.validate(image.root.parent.parent)


def test_development_cannot_bypass_a_dangling_release_selector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared import runtime_interpreter

    monkeypatch.setattr(runtime_interpreter, "WHEEL_RUNTIME", False)
    (tmp_path / "releases").mkdir()
    (tmp_path / "releases/current-release").symlink_to(tmp_path / "absent")
    with pytest.raises(ReleaseRejectedError, match="cannot start from development"):
        start_runtime.StartRuntime.development(tmp_path).validate(tmp_path)


def test_release_generation_never_reads_git_and_binds_image_identity(
    image: VerifiedRelease,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _admit(image)
    assert runtime.home is not None

    def no_git(_repo: Path) -> str:
        raise AssertionError("sealed image must not call Git")

    monkeypatch.setattr(_start_generation, "source_digest", no_git)
    initial = _start_generation.launch_digest(
        runtime.code_root, {}, home=runtime.home, runtime=runtime
    )
    other = replace(runtime, release=replace(image, manifest_digest="f" * 64))
    assert (
        _start_generation.launch_digest(runtime.code_root, {}, home=runtime.home, runtime=other)
        != initial
    )


@pytest.mark.parametrize(
    "command",
    [
        "{python} -m gateway",
        "{python} -I -B -X utf8 -m absent",
        "{python} -I -B -X utf8 -m gateway && echo bypass",
        "{other} -m gateway",
    ],
)
def test_release_manifest_rejects_unisolated_missing_or_shell_entry(
    image: VerifiedRelease,
    command: str,
) -> None:
    runtime = _admit(image)
    spec = ServiceSpec(
        session="gateway",
        cmd=command.format(
            python=shlex.quote(str(image.interpreter)),
            other=shlex.quote(str(image.interpreter.with_name("other-python"))),
        ),
        capabilities=frozenset({"gateway"}),
        requires_db=True,
    )
    with pytest.raises(ReleaseRejectedError):
        manifests.build_units(
            [spec], capabilities=["gateway"], repo_root=runtime.code_root, release=image
        )


def test_release_identity_does_not_write_binding_into_sealed_package(
    image: VerifiedRelease,
) -> None:
    from cli.start_identity import IdentityInput, prepare_identity

    runtime = _admit(image)
    assert runtime.home is not None
    home = runtime.home
    (home / ".env").write_text("AVA_MACHINE_NAME=fixture\nAVA_GATEWAY_URL=http://127.0.0.1:1\n")
    before = {path: path.read_bytes() for path in image.root.rglob("*") if path.is_file()}
    prepare_identity(
        IdentityInput(
            home,
            home.parent / "registry.json",
            runtime.code_root,
            False,
            frozenset({"agent-runner"}),
            {"AVA_MACHINE_NAME": "fixture"},
            runtime=runtime,
        )
    )
    assert {path: path.read_bytes() for path in image.root.rglob("*") if path.is_file()} == before
    assert not (runtime.code_root / ".ava_home.lock").exists()


def test_release_cold_start_uses_same_storage_readiness_without_source_or_schema_writes(
    image: VerifiedRelease,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    import cli.commands._converge as _converge_commands
    import cli.commands._repo as _repo_commands
    from cli.commands import _converge_extensions, _data_plane

    start = importlib.import_module("cli.commands.start")
    runtime = _admit(image)
    calls: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("retained start cannot mutate source/schema")

    def native_storage() -> int:
        calls.append("storage")
        return 0

    def pooler(*, refresh_schema: bool = True) -> None:
        assert not refresh_schema
        calls.append("pooler+consumer-readiness")

    def schema() -> int:
        calls.append("schema-readback")
        return 0

    monkeypatch.setattr(_converge_commands, "converge_host", forbidden)
    monkeypatch.setattr(_repo_commands, "_assert_schema_current_or_die", schema)
    monkeypatch.setattr(start, "_ensure_gateway_data_plane", native_storage)
    monkeypatch.setattr(start, "cmd_migrations_apply", forbidden)
    monkeypatch.setattr(_data_plane, "prepare_gateway_schema", forbidden)
    monkeypatch.setattr(_data_plane, "complete_gateway_data_plane", pooler)
    monkeypatch.setattr(_converge_extensions, "adopt_local_extensions", forbidden)
    monkeypatch.setattr(_converge_extensions, "materialize_cluster_extensions", forbidden)
    assert (
        start._prepare_cold_start(
            runtime.code_root,
            frozenset({"gateway"}),
            (),
            runtime=runtime,
        )
        == 0
    )
    assert calls == ["storage", "pooler+consumer-readiness", "schema-readback"]


def _operation_fixture(image: VerifiedRelease, phase: str) -> SimpleNamespace:
    home = image.root.parent.parent
    reference = SimpleNamespace(
        artifact_digest=image.digest,
        schema_digest=hashlib.sha256(b"baseline").hexdigest(),
        source_commit="b" * 40,
    )

    def verify(_home: Path, _platform: str) -> VerifiedRelease:
        return image

    reference.verify = verify
    request = SimpleNamespace(
        home=str(home),
        registry=str(home.parent / "registry.json"),
        platform_tag=platform.platform(),
        candidate=reference,
        previous=reference,
        configuration_digest=configuration_digest(home),
    )

    def require_captured_configuration() -> None:
        require_configuration(home, request.configuration_digest)

    request.require_configuration = require_captured_configuration
    return SimpleNamespace(
        phase=phase,
        request=request,
        direction="candidate",
        pitr=None,
        # The recorded executor kind selects the Linux boot-unit root owner.
        launch={"kind": "linux-systemd-v1"},
        reference=reference,
        require_configuration=require_captured_configuration,
    )


@pytest.mark.parametrize("foreign_executable", [False, True])
def test_operation_preflight_checks_actual_roster_without_selection_or_effects(
    image: VerifiedRelease,
    monkeypatch: pytest.MonkeyPatch,
    foreign_executable: bool,
) -> None:
    from cli.commands import _repo
    from cli.release_transition import stage
    from shared import machine

    home = image.root.parent.parent
    (image.root.parent / "current-release").unlink()
    # stage.preflight_operation writes AVA_HOME/AVA_CLUSTER_REGISTRY straight into the
    # live os.environ (cli/release_transition/stage.py), not through Settings, so the
    # raw-env seam (not monkeypatch.setenv) is what actually restores it after the test.
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.setitem(os.environ, "AVA_CLUSTER_REGISTRY", str(home.parent / "registry.json"))

    def operation(_path: Path) -> SimpleNamespace:
        return _operation_fixture(image, "prepared")

    spec = ServiceSpec(
        session="gateway",
        capabilities=frozenset({"gateway"}),
        requires_db=True,
        cmd="/outside-image/python -m gateway"
        if foreign_executable
        else ".venv/bin/python -m gateway",
    )

    def annotated(_roles: object) -> tuple[tuple[ServiceSpec, None], ...]:
        return ((spec, None),)

    def roles() -> frozenset[str]:
        return frozenset({"gateway"})

    def environment() -> dict[str, str]:
        return {}

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("read-only preflight cannot start a root")

    monkeypatch.setattr(stage, "read_operation", operation)
    monkeypatch.setattr(machine, "machine_role", roles)
    monkeypatch.setattr(_repo, "_services_for_roles_annotated", annotated)
    monkeypatch.setattr(_root_driver, "_root_child_env", environment)
    monkeypatch.setattr(_root_driver, "_bring_up_root", forbidden)
    before = {path: path.read_bytes() for path in home.rglob("*") if path.is_file()}
    if foreign_executable:
        with pytest.raises((ReleaseRejectedError, FileNotFoundError)):
            stage.preflight_operation(home / "operation.json")
    else:
        assert stage.preflight_operation(home / "operation.json", previous=True) == 0
    assert {path: path.read_bytes() for path in home.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("failed", [None, "gateway", "delivery-watchdog", "configuration"])
def test_operation_observation_requires_every_selected_service_ready(
    image: VerifiedRelease,
    monkeypatch: pytest.MonkeyPatch,
    failed: str | None,
) -> None:
    from cli.commands import _repo
    from cli.release_transition import stage
    from shared import machine, os_boot_unit
    from shared.machine import MachineRole
    from shared.native_process.ownership import OwnedProcess
    from shared.root_control import client

    home = image.root.parent.parent
    # Same raw-env seam as above: stage.preflight_operation writes these directly to
    # os.environ, not Settings.
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.setitem(os.environ, "AVA_CLUSTER_REGISTRY", str(home.parent / "registry.json"))
    runtime = _admit(image)
    roles: frozenset[MachineRole] = frozenset({"gateway"})
    roster = tuple(
        ServiceSpec(
            session=name, cmd=".venv/bin/python -m gateway", capabilities=roles, requires_db=True
        )
        for name in ("gateway", "delivery-watchdog")
    )
    root = OwnedProcess(1234, 5678.0, 90)
    cgroup = f"/system.slice/{os_boot_unit.unit_name(home)}"
    observed: list[str] = []

    def operation(_path: Path) -> SimpleNamespace:
        return _operation_fixture(image, "observing")

    def admitted(
        specs: tuple[ServiceSpec, ...], repo: Path, actual_roles: frozenset[str], **kwargs: object
    ) -> bool:
        assert specs == roster and repo == runtime.code_root and actual_roles == roles
        assert kwargs == {"reconcile": True, "runtime": runtime}
        observed.append("admitted")
        return True

    def readiness(specs: tuple[ServiceSpec, ...], *, timeout_s: int) -> SimpleNamespace:
        assert specs == roster and timeout_s == 60
        observed.append("full-roster-readiness")
        if failed == "configuration":
            (home / ".env").write_text("AVA_MACHINE_NAME=changed-after-health\n")
        return SimpleNamespace(
            unready=["gateway"] if failed == "gateway" else [],
            non_critical_unready=["delivery-watchdog"] if failed == "delivery-watchdog" else [],
        )

    def annotated(_roles: object) -> tuple[tuple[ServiceSpec, None], ...]:
        return tuple((spec, None) for spec in roster)

    def native_properties(_home: Path) -> dict[str, str]:
        return {
            "MainPID": str(root.pid),
            "ControlPID": "0",
            "ActiveState": "active",
            "ControlGroup": cgroup,
        }

    def process_group(pid: int) -> str:
        assert pid == root.pid
        return cgroup

    monkeypatch.setattr(stage, "read_operation", operation)
    monkeypatch.setattr(machine, "machine_role", lambda: roles)
    monkeypatch.setattr(_repo, "_services_for_roles_annotated", annotated)
    monkeypatch.setattr(_root_driver, "admit_live_start", admitted)
    monkeypatch.setattr(_root_driver, "_wait_for_service_tree", readiness)
    monkeypatch.setattr(client, "root_process", lambda: root)
    monkeypatch.setattr(os_boot_unit, "_manager_properties", native_properties)
    monkeypatch.setattr(os_boot_unit, "_process_cgroup", process_group)
    if failed is None:
        assert stage.observe_operation(home / "operation.json") == 0
    elif failed == "configuration":
        with pytest.raises(ReleaseRejectedError, match="configuration changed"):
            stage.observe_operation(home / "operation.json")
    else:
        with pytest.raises(RuntimeError, match="incomplete service readiness"):
            stage.observe_operation(home / "operation.json")
    assert observed == ["admitted", "full-roster-readiness"]


@pytest.mark.parametrize("change_at", ["before-identity", "before-settings"])
def test_release_run_start_checks_configuration_before_identity_and_settings(
    image: VerifiedRelease,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change_at: str,
) -> None:
    from cli import main, start_intent
    from cli.release_transition.journal import create
    from cli.release_transition.request import ReleaseRef, Request
    from shared.release_operation import authorized_start

    home = image.root.parent.parent
    runtime = _admit(image)
    reference = ReleaseRef(
        artifact_digest=image.digest,
        manifest_digest=image.manifest_digest,
        schema_digest=hashlib.sha256(b"baseline").hexdigest(),
        source_commit="b" * 40,
    )
    request = Request(
        id=uuid4(),
        home=str(home),
        registry=str(home.parent / "registry.json"),
        created_at=datetime.now(UTC),
        platform_tag=platform.platform(),
        machine="fixture",
        previous=reference.model_copy(update={"artifact_digest": "f" * 64}),
        candidate=reference,
        executor=reference,
        configuration_digest=configuration_digest(home),
    )
    selector = home / "releases" / "current-release"
    selected = selector.read_bytes()
    selector.write_text(
        json.dumps(
            {
                "artifact_digest": request.previous.artifact_digest,
                "manifest_digest": request.previous.manifest_digest,
            }
        )
    )
    create(request)
    selector.write_bytes(selected)
    state = json.loads(request.path.read_bytes())
    state["phase"] = "starting"
    request.path.write_text(json.dumps(state))
    prepared: list[bool] = []

    def change() -> None:
        (home / ".env").write_text("AVA_MACHINE_NAME=changed-before-settings\n")

    def prepare(_args: Namespace, _home: Path, _runtime: start_runtime.StartRuntime) -> None:
        assert change_at == "before-settings"
        prepared.append(True)
        change()

    def forbidden_logging() -> None:
        raise AssertionError("changed configuration reached Settings-dependent startup")

    # start_intent.run_start's whole point here is a pre-Settings identity bootstrap
    # (cli/start_intent.py): it reads AVA_HOME/AVA_CLUSTER_REGISTRY from the live
    # os.environ before Settings loads, so the raw-env seam is the real one, and the
    # test asserts a later env change never reaches the Settings-dependent path.
    monkeypatch.setitem(os.environ, "AVA_HOME", str(home))
    monkeypatch.setitem(os.environ, "AVA_CLUSTER_REGISTRY", request.registry)
    monkeypatch.setattr(start_intent, "_prepare_start_locked", prepare)
    monkeypatch.setattr(main, "_init_detached_cli_logging", forbidden_logging)
    if change_at == "before-identity":
        change()
    with authorized_start(request.path):
        assert start_intent.run_start(Namespace(worktree=False), runtime=runtime) == 1
    assert prepared == ([True] if change_at == "before-settings" else [])
    assert "configuration changed after preparation" in capsys.readouterr().err
