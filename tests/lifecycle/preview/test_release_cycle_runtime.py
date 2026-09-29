"""Native completion, steady boot and durable completed-work proof boundaries."""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest

from cli.release_fleet.request import FleetRequest
from cli.release_transition import root_service
from cli.release_transition.journal import Operation, Retirement
from cli.release_transition.launcher_linux import LinuxJob
from cli.release_transition.native import LINUX
from cli.release_transition.request import ReleaseRef
from scripts.preview import release_cycle_runtime as runtime
from scripts.preview import release_cycle_state as state
from scripts.preview import release_generation
from shared.native_process.ownership import OwnedProcess
from shared.os_boot_unit import BootStartAction, BootUnitContext
from shared.runtime_release import VerifiedRelease
from tests.lifecycle.transition.phases import at_phase
from tests.lifecycle.transition.test_journal import request_record as request_record


def _native(*, live: bool = False, failed: bool = False) -> LinuxJob:
    return LinuxJob(
        unit="ava-update.fixture.a0.service",
        boot_id="boot",
        invocation_id="a" * 32,
        cgroup="/system.slice/ava-update.fixture.a0.service",
        owner=OwnedProcess(900, 1.0, 20) if live else None,
        active="active",
        sub="running" if live else "exited",
        result="exit-code" if failed else "success",
        exit_code=0 if live else 1,
        exit_status=1 if failed else 0,
    )


@pytest.mark.parametrize("change", [None, "live", "rollback", "incomplete", "error", "failed"])
def test_only_closed_successful_requested_transition_counts_as_complete(
    request_record: FleetRequest, change: str | None
) -> None:
    operation = at_phase(
        "observing" if change == "incomplete" else "complete",
        request=request_record,
        direction="previous" if change == "rollback" else "candidate",
        error="retained error" if change == "error" else None,
    )
    native = _native(live=change == "live", failed=change == "failed")
    if change in {"rollback", "incomplete", "error", "failed"}:
        with pytest.raises(RuntimeError):
            runtime._completed(operation, native, cleanup=False)
    else:
        assert runtime._completed(operation, native, cleanup=False) is (change is None)


def test_already_retired_previous_operation_never_reacquires_mutation_authority(
    request_record: FleetRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = _native()
    operation = at_phase(
        "complete",
        request=request_record,
        launch={"kind": LINUX, "inert": True},
        launch_attempted=True,
        retirement=Retirement(terminal=native.model_dump(mode="json"), state="absent"),
    )

    def refuse(_record: object) -> NoReturn:
        pytest.fail("old operation cannot regain active authority")

    monkeypatch.setattr(runtime, "retire_current", refuse)
    assert runtime._completed(operation, native, cleanup=True)


@pytest.mark.parametrize("wrong", [False, True])
def test_steady_boot_uses_verified_pinned_image_in_existing_home_unit(
    request_record: FleetRequest, monkeypatch: pytest.MonkeyPatch, *, wrong: bool
) -> None:
    home = Path(request_record.home)
    root = home / "releases" / request_record.previous.artifact_digest
    image = VerifiedRelease(
        request_record.previous.artifact_digest,
        request_record.previous.manifest_digest,
        root,
        root / "venv/bin/python",
        root / "site",
    )
    other = VerifiedRelease("9" * 64, image.manifest_digest, root, image.interpreter, image.cwd)

    def verify(*_args: object) -> VerifiedRelease:
        return other if wrong else image

    monkeypatch.setattr(ReleaseRef, "verify", verify)
    calls: list[tuple[BootUnitContext, BootStartAction]] = []

    def install(*, context: BootUnitContext, action: BootStartAction) -> None:
        calls.append((context, action))

    monkeypatch.setattr(root_service, "install", install)
    if wrong:
        with pytest.raises(ValueError, match="differs"):
            root_service.install_steady(
                home, Path(request_record.registry), request_record.previous, image
            )
        assert not calls
    else:
        root_service.install_steady(
            home, Path(request_record.registry), request_record.previous, image
        )
        context, action = calls[0]
        assert context.home == home and context.registry == Path(request_record.registry)
        assert action.argv[:7] == (*image.module_argv("cli.release_transition.boot"),)
        assert action.cwd == image.cwd and "--operation" not in action.argv
        assert (
            "--artifact" in action.argv and request_record.previous.artifact_digest in action.argv
        )
        assert dict(action.environment)["AVA_HOME"] == request_record.home
        assert "PYTHONPATH" not in dict(action.environment)


def test_initial_selects_through_the_release_adopt_operator_verb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`initial()` used to call `activate_release`/`install_steady` inline; it
    now invokes `ava cluster release adopt` — the operator verb that performs
    the exact same sequence (see `cli/release_operator/adopt.py`) — as the
    real public CLI, in the selected image's own interpreter. Mirrors
    `test_dispatch_reverifies_image_and_invokes_only_public_cli_with_clean_environment`
    below for the same reason: only the public CLI surface is trusted for
    effects, never a private in-process call."""
    import subprocess

    run = tmp_path.resolve()
    (run / "home").mkdir()
    receipt = run / "previous-receipt.json"
    receipt.write_text("{}")
    (run / "release-inputs.json").write_text(
        json.dumps({"images": {"a": {"receipt": str(receipt)}}})
    )
    image = VerifiedRelease(
        "a" * 64, "b" * 64, run / "image", run / "image/venv/bin/python", run / "image/site"
    )
    events: list[str] = []

    def verified(_run: Path, name: str) -> tuple[ReleaseRef, VerifiedRelease]:
        assert name == "a"
        events.append("verify")
        return (
            ReleaseRef(
                artifact_digest="a" * 64,
                manifest_digest="b" * 64,
                schema_digest="c" * 64,
                source_commit="d" * 40,
            ),
            image,
        )

    def command(argv: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        events.append("adopt")
        assert argv == image.module_argv(
            "cli.main", "cluster", "release", "adopt", "--receipt", str(receipt)
        )
        assert kwargs["cwd"] == image.cwd and kwargs["check"]
        assert kwargs["env"]["AVA_HOME"] == str(run / "home")
        assert kwargs["env"]["AVA_CLUSTER_REGISTRY"] == str(run / "clusters.json")
        assert not {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}.intersection(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(runtime, "image_input", verified)
    monkeypatch.setattr(runtime.subprocess, "run", command)

    runtime.initial(run)

    assert events == ["verify", "adopt"]


def test_retained_agent_checkpoint_change_cannot_be_hidden_by_same_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = {"agent": 5, "sha256": "before", "rows": {"checkpoints": 1}}
    (tmp_path / "release-frozen-a.json").write_text(
        json.dumps({"result": "passed", "agent": 5, "state": previous})
    )

    def changed(_run: Path, _agent: int) -> dict[str, Any]:
        return previous | {"sha256": "after"}

    monkeypatch.setattr(state, "state", changed)
    with pytest.raises(RuntimeError, match="identity/checkpoints changed"):
        state.verify(tmp_path, "b")
    evidence = json.loads((tmp_path / "release-state-b.json").read_text())
    assert evidence["result"] == "failed" and evidence["agents"][0]["sha256"] == "after"


def test_termination_acceptance_never_substitutes_for_native_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from cli.commands.lifecycle import service_stop

    (tmp_path / "smoke-release-a.json").write_text('{"agent": 5}')
    (tmp_path / "config.json").write_text(
        json.dumps({"gateway_url": "http://127.0.0.1:5010", "ports": {"gateway": 5010}})
    )
    calls: list[dict[str, Any]] = []

    def accepted(url: str, **kwargs: Any) -> httpx.Response:
        calls.append(kwargs)
        return httpx.Response(200, json={"status": "enqueued"}, request=httpx.Request("POST", url))

    def retained() -> None:
        raise RuntimeError("native execution resource remains")

    clock = iter((0.0, 100.0))
    monkeypatch.setattr(state.httpx, "post", accepted)

    def closed(_run: Path, _agent: int) -> dict[str, Any]:
        return {"agent": 5, "sha256": "closed-row"}

    monkeypatch.setattr(state, "state", closed)
    monkeypatch.setattr(state.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(service_stop, "require_no_terminals", retained)
    with pytest.raises(TimeoutError, match="native closure"):
        state.freeze(tmp_path, "a")
    assert len(calls) == 1 and calls[0]["json"] == {"force": False}
    evidence = json.loads((tmp_path / "release-frozen-a.json").read_text())
    assert evidence["result"] == "failed" and "native execution" in evidence["pending"]


class _TerminatedAgent:
    """The proof connection over agent 5: `terminated`, `closed_at` left NULL.

    Terminate has no closed state (decisions/2026-09-27-terminate-has-no-closed-state.md),
    so nothing stamps `agents_meta.closed_at` any more.
    """

    columns: dict[str, object] = {  # noqa: RUF012 — a read-only fixture row
        "a.id": 5,
        "a.created_at": "2026-09-28T00:00:00+00:00",
        "m.machine": "proof",
        "m.born_spawner": "user",
        "m.birth_config": "{}",
        "m.config_overlay": "{}",
        "m.status": "terminated",
        "m.closed_at": None,
    }

    def __init__(self) -> None:
        self.read_only = False
        self.isolation_level: object = None
        self.statements: list[object] = []

    def __enter__(self) -> _TerminatedAgent:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, query: object, _params: tuple[object, ...] = ()) -> Any:
        self.statements.append(query)
        if not isinstance(query, str):  # a checkpoint table's rows
            return iter([('{"checkpoint": 1}',)])
        if query.startswith("SET LOCAL "):
            return None
        selected = query.removeprefix("SELECT ").split(" FROM ")[0].split(", ")
        row = tuple(self.columns[column] for column in selected)
        return SimpleNamespace(fetchone=lambda: row)


def test_a_terminated_agent_is_durably_closed_without_a_closed_at_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def connection(_context: object) -> _TerminatedAgent:
        return _TerminatedAgent()

    monkeypatch.setattr(release_generation.Context, "read_only", connection)
    observed = state.state(tmp_path, 5)
    assert observed["agent"] == 5 and observed["rows"]["checkpoints"] == 1


def test_retained_state_is_read_as_the_administrator_never_the_source_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once an image is selected the source checkout is no admitted runtime and
    holds only the credential-free pooler endpoint: dialing it fails with
    `fe_sendauth: no password supplied` (release proof r3, `state --label a`).
    Retained state is read by the OS-user administrator over this home's
    owner-only socket, bound to its postmaster, in one read-only snapshot."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    from shared import pg_admin
    from shared.config import settings

    run = tmp_path.resolve()
    (run / "home").mkdir()
    (run / "home/.env").write_text("AVA_DB_URL=postgresql://127.0.0.1:6432/ava_preview\n")
    (run / "config.json").write_text(json.dumps({"ports": {"postgres": 5433, "pgbouncer": 6432}}))
    monkeypatch.setattr(settings.data_plane, "db_url", "postgresql://127.0.0.1:6432/ava_preview")

    def unauthenticated(*_args: object, **_kwargs: object) -> NoReturn:
        raise psycopg.OperationalError("fe_sendauth: no password supplied")

    monkeypatch.setattr(psycopg, "connect", unauthenticated)
    connection = _TerminatedAgent()
    dials: list[tuple[dict[str, Any], Path | None]] = []

    @contextmanager
    def administrator(
        url: str, *, expected_data_dir: Path | None = None, **_kwargs: object
    ) -> Generator[_TerminatedAgent]:
        dials.append((conninfo_to_dict(url), expected_data_dir))
        yield connection

    socket = run / "socket"

    def socket_url(port: int) -> str:
        return f"postgresql://os-user@/postgres?host={socket}&port={port}"

    monkeypatch.setattr(pg_admin, "connect", administrator)
    monkeypatch.setattr(pg_admin, "pg_admin_url", socket_url)
    assert state.state(run, 5)["rows"]["checkpoints"] == 1
    assert dials == [
        (
            {"user": "os-user", "host": str(socket), "port": "5433", "dbname": "ava_preview"},
            run / "home/pg",
        )
    ]
    assert connection.read_only
    assert connection.isolation_level == psycopg.IsolationLevel.REPEATABLE_READ
    assert connection.statements[0] == "SET LOCAL statement_timeout = '5s'"


def _cycle_inputs(run: Path, request: FleetRequest) -> None:
    """Captured images A (the request's previous) and B (its candidate), no request yet."""
    images = {
        name: {
            "receipt": str(run / f"receipt-{name}.json"),
            "reference": reference.model_dump(mode="json"),
            "runtime": {},
            "fixture": {},
        }
        for name, reference in (("a", request.previous), ("b", request.candidate))
    }
    (run / "release-inputs.json").write_text(json.dumps({"images": images, "requests": {}}))


def _verified(
    run: Path, request: FleetRequest
) -> tuple[dict[str, ReleaseRef], dict[str, VerifiedRelease]]:
    """The captured references A and B and their verified images."""
    references = {"a": request.previous, "b": request.candidate}
    images = {
        name: VerifiedRelease(
            reference.artifact_digest,
            reference.manifest_digest,
            run / name,
            run / name / "venv/bin/python",
            run / name / "site",
        )
        for name, reference in references.items()
    }
    return references, images


def _select(run: Path, artifact: str, manifest: str) -> None:
    """The home's release selection, as a completed transition leaves it."""
    (run / "home/releases/current-release").write_text(
        json.dumps({"artifact_digest": artifact, "manifest_digest": manifest})
    )


def _captured_request(request: FleetRequest, label: str = "ab") -> Path:
    import hashlib

    run = Path(request.home).parent
    _cycle_inputs(run, request)
    path = run / f"release-{label}-request.json"
    path.write_text(request.model_dump_json() + "\n")
    inputs = json.loads((run / "release-inputs.json").read_text())
    inputs["requests"][label] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    (run / "release-inputs.json").write_text(json.dumps(inputs))
    return run


def test_dispatch_reverifies_the_executor_and_submits_as_the_admitted_image(
    request_record: FleetRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`update --prepared` is the previous image's half of the handoff: the
    home's admitted image (A, selected) receives the database login, verifies
    the executor and execs its `submit` entry with that login. The executor
    image B is not selected and holds none (release proof r4)."""
    import subprocess

    run = _captured_request(request_record)
    references, images = _verified(run, request_record)
    events: list[str] = []

    def verified(_run: Path, name: str) -> tuple[ReleaseRef, VerifiedRelease]:
        events.append(f"verify-{name}")
        return references[name], images[name]

    def command(argv: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        events.append("public CLI")
        assert argv == images["a"].module_argv(
            "cli.main", "cluster", "update", "--prepared", str(run / "release-ab-request.json")
        )
        assert kwargs["cwd"] == images["a"].cwd and kwargs["check"] and kwargs["timeout"] == 180
        assert kwargs["env"]["AVA_HOME"] == request_record.home
        assert kwargs["env"]["AVA_CLUSTER_REGISTRY"] == request_record.registry
        assert not {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "OPENAI_API_KEY"}.intersection(
            kwargs["env"]
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(runtime, "image_input", verified)
    monkeypatch.setattr(runtime.subprocess, "run", command)
    runtime.dispatch(run, "ab")
    assert events == ["verify-b", "verify-a", "public CLI"]
    (run / "release-ab-request.json").write_text(
        request_record.model_copy(update={"machine": "changed"}).model_dump_json()
    )
    with pytest.raises(RuntimeError, match="request changed"):
        runtime.dispatch(run, "ab")
    assert events == ["verify-b", "verify-a", "public CLI", "verify-b"]


def test_fixture_runs_isolated_before_trusting_its_installed_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import subprocess

    image = VerifiedRelease(
        "a" * 64,
        "b" * 64,
        tmp_path / "image",
        tmp_path / "image/venv/bin/python",
        tmp_path / "image/site",
    )
    path = tmp_path / "source/tests/e2e/fakes/scenarios/message_flow.py"
    path.parent.mkdir(parents=True)
    path.write_text("source scenario")
    observed = {
        "files": {
            "tests.e2e.fakes.scenarios.message_flow": hashlib.sha256(path.read_bytes()).hexdigest()
        },
        "reply_sha256": "reply",
    }
    calls: list[list[str]] = []

    def command(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert argv[:6] == [str(image.interpreter), "-I", "-B", "-X", "utf8", "-c"]
        assert "is_relative_to(root)" in argv[6]
        assert "model.invoke([])" in argv[6] and "print(1 + 2)" in argv[6]
        assert kwargs["cwd"] == image.cwd and "PYTHONPATH" not in kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, json.dumps(observed), "")

    monkeypatch.setattr(runtime.subprocess, "run", command)
    assert runtime._fixture(tmp_path, image) == observed
    path.write_text("different scenario")
    with pytest.raises(RuntimeError, match="scripted fixtures differ"):
        runtime._fixture(tmp_path, image)
    assert len(calls) == 2


@pytest.mark.parametrize("changed_attempt", [False, True])
def test_executor_finishing_between_journal_and_native_reads_uses_final_same_attempt(
    request_record: FleetRequest, monkeypatch: pytest.MonkeyPatch, *, changed_attempt: bool
) -> None:
    before = at_phase(
        "resuming",
        request=request_record,
        launch={"kind": LINUX, "unit": "same"},
        launch_attempted=True,
    )
    after = before.model_copy(
        update={
            "phase": "complete",
            "launch": {"kind": LINUX, "unit": "other"} if changed_attempt else before.launch,
        }
    )
    snapshots = iter((before, after))

    def read(_path: Path) -> Operation:
        return next(snapshots)

    def native(_record: object) -> LinuxJob:
        return _native()

    monkeypatch.setattr(runtime, "read_operation", read)
    monkeypatch.setattr(runtime, "readback", native)
    if changed_attempt:
        with pytest.raises(RuntimeError, match="identity changed"):
            runtime._sample(request_record, cleanup=False)
    else:
        observed, job = runtime._sample(request_record, cleanup=False)
        assert observed == after and job is not None
        assert runtime._completed(observed, job, cleanup=False)


@pytest.mark.parametrize("outcome", ["closed", "survivor", "unknown"])
def test_prior_generation_closure_records_every_captured_native_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    from dataclasses import asdict

    import psutil

    from scripts.preview import release_cycle_custody as custody

    root = OwnedProcess(800, 1.0, 1)
    unit = OwnedProcess(801, 2.0, 2)
    descendant = OwnedProcess(802, 3.0, 3)
    (tmp_path / "cycle-release-a.json").write_text(
        json.dumps({"result": "passed", "births": {"root": asdict(root), "unit": asdict(unit)}})
    )

    def tree(_root: OwnedProcess) -> set[OwnedProcess]:
        return {root, unit, descendant}

    def alive(_owner: OwnedProcess) -> bool:
        return True

    monkeypatch.setattr(custody, "capture_tree", tree)
    monkeypatch.setattr(OwnedProcess, "live", alive)
    custody.capture(tmp_path, "a")

    def after(owner: OwnedProcess) -> bool:
        if outcome == "unknown" and owner == descendant:
            raise psutil.AccessDenied(owner.pid)
        return outcome == "survivor" and owner == descendant

    monkeypatch.setattr(OwnedProcess, "live", after)
    if outcome == "closed":
        custody.closed(tmp_path, "a")
    else:
        with pytest.raises((RuntimeError, psutil.AccessDenied)):
            custody.closed(tmp_path, "a")
    evidence = json.loads((tmp_path / "release-apps-a-closed.json").read_text())
    assert evidence["result"] == ("passed" if outcome == "closed" else "failed")
    if outcome != "unknown":
        assert len(evidence["observations"]) == 3


def test_dispatch_builds_and_submits_as_the_admitted_image_on_both_submissions(
    request_record: FleetRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A→B's request is built once and first submitted by image A, the admitted
    runtime; after the transition selects B, the completed-only retirement
    resubmission runs as B. Every later dispatch reuses the captured bytes."""
    import subprocess

    run = Path(request_record.home).parent
    _cycle_inputs(run, request_record)
    references, images = _verified(run, request_record)
    monkeypatch.setattr(runtime, "image_input", lambda _run, name: (references[name], images[name]))
    out = run / "release-ab-request.json"
    calls: list[tuple[str, ...]] = []

    def command(argv: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert kwargs["env"]["AVA_HOME"] == str(run / "home") and "PYTHONPATH" not in kwargs["env"]
        if "request" in argv:
            out.write_text(request_record.model_dump_json() + "\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(runtime.subprocess, "run", command)
    runtime.dispatch(run, "ab")
    _select(run, request_record.candidate.artifact_digest, request_record.candidate.manifest_digest)
    runtime.dispatch(run, "ab")  # the completed-only retirement resubmission
    submit = ("cli.main", "cluster", "update", "--prepared", str(out))
    assert calls == [
        images["a"].module_argv(
            "cli.main",
            "cluster",
            "release",
            "request",
            "--commit",
            request_record.candidate.source_commit,
            "--receipt",
            str(run / "receipt-b.json"),
            "--out",
            str(out),
            "--watch-s",
            "30",
        ),
        images["a"].module_argv(*submit),
        images["b"].module_argv(*submit),
    ]
    recorded = json.loads((run / "release-inputs.json").read_text())["requests"]["ab"]
    assert recorded["operation"] == str(request_record.path)
    runtime.wait_executor(run, "ba", cleanup=True)  # never requested: nothing to settle


@pytest.mark.parametrize("selected", [None, "a", "b", "foreign"])
def test_the_cli_runs_as_the_homes_admitted_runtime(
    request_record: FleetRequest, monkeypatch: pytest.MonkeyPatch, selected: str | None
) -> None:
    """The home's selection decides admission (`require_admitted_runtime`): the
    source checkout before any image, else exactly the selected captured image,
    verified again. A selection outside the captured pair is refused unrun."""
    import subprocess

    run = Path(request_record.home).parent
    _cycle_inputs(run, request_record)
    references, images = _verified(run, request_record)
    verified: list[str] = []

    def image_input(_run: Path, name: str) -> tuple[ReleaseRef, VerifiedRelease]:
        verified.append(name)
        return references[name], images[name]

    monkeypatch.setattr(runtime, "image_input", image_input)
    if selected is None:
        (run / "home/releases/current-release").unlink()
    elif selected == "foreign":
        _select(run, "0" * 64, "b" * 64)
    else:
        _select(run, references[selected].artifact_digest, references[selected].manifest_digest)
    calls: list[tuple[tuple[str, ...], Path]] = []

    def command(argv: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((tuple(argv), kwargs["cwd"]))
        assert kwargs["check"]
        assert kwargs["env"]["AVA_HOME"] == str(run / "home")
        assert kwargs["env"]["AVA_CLUSTER_REGISTRY"] == str(run / "clusters.json")
        assert not {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}.intersection(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(runtime.subprocess, "run", command)
    if selected == "foreign":
        with pytest.raises(RuntimeError, match="captured"):
            runtime.admitted_cli(run, "stop", "-y", "--stop-browser", timeout=900)
        assert not calls
        return
    runtime.admitted_cli(run, "stop", "-y", "--stop-browser", timeout=900)
    if selected is None:
        source = run / "source"
        expected = (
            str(source / ".venv/bin/python"),
            "-m",
            "cli.main",
            "stop",
            "-y",
            "--stop-browser",
        )
        assert calls == [(expected, source)] and not verified
    else:
        image = images[selected]
        argv = image.module_argv("cli.main", "stop", "-y", "--stop-browser")
        assert calls == [(argv, image.cwd)] and verified == [selected]


@pytest.mark.parametrize(
    ("action", "arguments"),
    [("stop", ("stop", "-y", "--stop-browser")), ("destroy", ("cluster", "destroy", "--path"))],
)
def test_cleanup_actions_are_the_ordinary_stop_and_destroy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, arguments: tuple[str, ...]
) -> None:
    run = tmp_path.resolve()
    calls: list[tuple[Path, tuple[str, ...]]] = []

    def admitted(target: Path, *argv: str, timeout: float) -> None:
        assert timeout == 900
        calls.append((target, argv))

    def own_preview(_run: Path) -> None:
        pass

    monkeypatch.setattr(runtime.sys, "platform", "linux")
    monkeypatch.setattr(runtime.sys, "argv", ["release_cycle_runtime", str(run), action])
    monkeypatch.setattr(runtime, "_require_context", own_preview)
    monkeypatch.setattr(runtime, "admitted_cli", admitted)
    runtime.main()
    home = (str(run / "home"),) if action == "destroy" else ()
    assert calls == [(run, (*arguments, *home))]


@pytest.mark.parametrize("change", ["commit", "schema"])
def test_the_cycle_admits_only_distinct_commits_over_one_schema_before_any_stop(
    request_record: FleetRequest, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    run = Path(request_record.home).parent
    b = request_record.candidate
    b = b.model_copy(
        update={"source_commit": request_record.previous.source_commit}
        if change == "commit"
        else {"schema_digest": "0" * 64}
    )
    refs = {"previous": request_record.previous, "candidate": b}
    monkeypatch.setattr("shared.os_boot_unit.systemd_running", lambda: True)
    monkeypatch.setattr("shared.os_boot_unit.unit_name", lambda _home: "ava-home.service")
    monkeypatch.setattr(runtime, "dotenv_values", lambda *_a, **_k: dict(runtime.local.PROFILE))
    monkeypatch.setattr(
        runtime, "captured", lambda _run, receipt, *_bind: (refs[receipt.name], receipt, {})
    )
    monkeypatch.setattr(runtime, "_fixture", lambda _run, _image: {"files": {}})
    monkeypatch.setattr(runtime, "current_pointer", lambda _store: None)
    monkeypatch.setattr(runtime, "sql_inventory", lambda _image: {"x.sql": "0" * 64})
    bindings = (("d", "c"), ("d", "c"))
    message = "distinct source commits" if change == "commit" else "same-schema releases only"
    with pytest.raises(ValueError, match=message):
        runtime.prepare(run, run / "previous", run / "candidate", bindings)
    assert not (run / "release-inputs.json").exists()
