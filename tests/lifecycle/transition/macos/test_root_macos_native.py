"""Opt-in native macOS release start through the persistent home helper: A -> B -> A.

A disposable, uniquely labelled helper job in ``gui/<uid>`` runs this checkout's
helper (ad hoc, or the stable identity with AVA_NATIVE_SIGNED_HELPER=1) for a
private home. Its plist lives beside that home, never in LaunchAgents. Two
minimal retained images carry a fixture root (the run-dir lock and K1 status
with its native birth) and a fixture stage that stands in for ordinary start:
it persists the image's seed and calls ``root_seed``, the same calls
``_seed_via_helper`` makes. Real: the helper keeper, kernel-peer and signature
authentication, ordinary root stop (``_stop_root_service_tree``), selector CAS,
the journal, ``drive`` and the helper root custody. Not exercised here: the
application start and readiness, the finite executor job around the start
(proven by test_launcher_macos_native.py), the data plane (its bracket is
replaced), logout and reboot. Cleanup stops the keeper's root, boots out the
exact label, then signals only captured births that are still live.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil
import pytest

from cli.commands import _root_driver
from cli.release_transition import journal, root_macos, root_service
from cli.release_transition import launcher_macos as macos
from cli.release_transition.execute import drive
from cli.release_transition.journal import Journal, Operation
from cli.release_transition.launchd_custody import RootCustody
from cli.release_transition.local import LocalTransition
from cli.release_transition.request import ReleaseRef, Request
from services.permissions_helper import client, lifecycle
from shared import paths
from shared.config import settings
from shared.native_process.ownership import OwnedProcess
from shared.runtime_release import VerifiedRelease, file_sha256
from tests.lifecycle.transition.macos import native_fixture

pytestmark = [
    pytest.mark.skipif(
        sys.platform != "darwin" or os.environ.get("AVA_NATIVE_RELEASE_START") != "1",
        reason="requires explicit disposable macOS helper-root fixture opt-in",
    ),
    pytest.mark.native_permissions_helper,
]

_PLATFORM = "macOS-fixture"
_SCHEMA = "c" * 64
_COMMIT = "d" * 40

_ROOT = """import argparse, fcntl, json, os, signal, socket, sys
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--run-dir', type=Path, required=True)
parser.add_argument('--manifests', type=Path, required=True)
parser.add_argument('--wiring', required=True)
args = parser.parse_args()
tag = (Path(__file__).resolve().parents[2] / 'fixture-image.txt').read_text().strip()
import psutil
lock = os.open(args.run_dir / 'ava-root.lock', os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.ftruncate(lock, 0)
os.write(lock, f'{os.getpid()}\\n'.encode())
identity = {'pid': os.getpid(), 'starttime': None,
            'create_time': float(psutil.Process()._proc.create_time(monotonic=True))}
path = args.run_dir / 'ava-root.sock'
path.unlink(missing_ok=True)
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(str(path))
os.chmod(path, 0o600)
server.listen(8)
server.settimeout(0.2)
stopping = []
signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
while not stopping:
    try:
        conn, _ = server.accept()
    except (TimeoutError, InterruptedError):
        continue
    with conn:
        conn.settimeout(5)
        data = b''
        while not data.endswith(b'\\n'):
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        try:
            verb = json.loads(data)['verb']
        except (ValueError, KeyError):
            verb = None
        if verb == 'status':
            reply = {'ok': True, 'result': {'root': identity, 'units': [], 'image': tag}}
        else:
            reply = {'ok': False, 'code': 'unknown_verb', 'error': f'fixture root: {verb!r}'}
        conn.sendall(json.dumps(reply).encode() + b'\\n')
path.unlink(missing_ok=True)
"""

_STAGE = """import argparse, json, os, socket, sys, time
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--operation', type=Path, required=True)
parser.add_argument('--observe', action='store_true')
args = parser.parse_args()
tag = (Path(__file__).resolve().parents[2] / 'fixture-image.txt').read_text().strip()
run = Path(os.environ['AVA_HOME']) / 'run' / 'ava-root'
def call(path, message, timeout):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(timeout)
        stream.connect(str(path))
        stream.sendall(json.dumps(message).encode() + b'\\n')
        data = b''
        while not data.endswith(b'\\n'):
            chunk = stream.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data)
def serving():
    try:
        reply = call(run / 'ava-root.sock', {'verb': 'status'}, 2.0)
    except (OSError, ValueError):
        return None
    return reply['result']['image'] if reply.get('ok') else None
def marker(name):
    return (run / f'fixture-{name}-{tag}').exists()
if args.observe:
    sys.exit(0 if serving() == tag else 'fixture stage: selected image is not serving')
current = serving()
if current == tag:
    sys.exit(0)
if current is not None:
    sys.exit(f'fixture stage: image {current} is serving')
if marker('stage-fail'):
    sys.exit('fixture stage: injected start failure before seeding')
(helper,) = list(run.parent.glob('permissions-helper.*.sock'))
seed = {'argv': [sys.executable, '-I', '-B', '-X', 'utf8', '-m', 'services.ava_root',
                 '--run-dir', str(run), '--manifests', str(run / 'manifests.json'),
                 '--wiring', 'fixture'],
        'cwd': os.getcwd(), 'run_dir': str(run), 'stdout': str(run / 'root.stdout.log'),
        'stderr': str(run / 'root.stderr.log'), 'env': {}}
staged = run / 'seed.json.fixture'
fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as stream:
    json.dump(seed, stream)
    stream.flush()
    os.fsync(stream.fileno())
os.replace(staged, run / 'seed.json')
reply = call(helper, {'id': 1, 'method': 'root_seed', 'config': seed}, 10.0)
if not reply.get('ok'):
    sys.exit(f'fixture stage: root_seed refused: {reply}')
deadline = time.monotonic() + 20
while serving() != tag:
    if time.monotonic() >= deadline:
        sys.exit('fixture stage: the seeded root did not serve')
    time.sleep(0.05)
if marker('stage-hang'):
    time.sleep(3600)
"""


def _image(home: Path, tag: str) -> ReleaseRef:
    """A complete minimal retained image: interpreter, psutil, fixture root and stage."""
    assert sys.version_info[:2] == (3, 12)
    staging = home / "releases" / f"staging-{tag}"
    binary = staging / "venv/bin/python"
    site = staging / "venv/lib/python3.12/site-packages"
    binary.parent.mkdir(parents=True)
    site.mkdir(parents=True)
    interpreter = Path(sys.executable).resolve()
    shutil.copyfile(interpreter, binary)
    binary.chmod(0o700)
    shutil.copyfile(
        interpreter.parent.parent / "lib/libpython3.12.dylib",
        staging / "venv/lib/libpython3.12.dylib",
    )
    (staging / "venv/pyvenv.cfg").write_text(
        f"home = {Path(sys.base_prefix) / 'bin'}\ninclude-system-site-packages = false\n"
    )
    shutil.copytree(
        Path(psutil.__file__).parent,
        site / "psutil",
        ignore=shutil.ignore_patterns("__pycache__", "tests"),
    )
    for package, module, body in (
        ("services/ava_root", "__main__.py", _ROOT),
        ("cli/release_transition", "stage.py", _STAGE),
    ):
        directory = site / package
        directory.mkdir(parents=True)
        for parent in (directory, directory.parent):
            (parent / "__init__.py").touch()
        (directory / module).write_text(body)
    (site / "fixture-image.txt").write_text(f"{tag}\n")
    (site / "migrations").mkdir()
    (site / "migrations/fixture.sql").write_text("SELECT 1;\n")
    (site / "shared").mkdir()
    (site / "shared/release-build.json").write_text(
        json.dumps(
            {
                "version": 1,
                "source_commit": _COMMIT,
                "source_tree": "e" * 40,
                "source_archive_digest": "f" * 64,
                "schema_digest": _SCHEMA,
                "applied_names": ["fixture.sql"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    files = {
        str(path.relative_to(staging)): file_sha256(path)
        for path in staging.rglob("*")
        if path.is_file()
    }
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    manifest = staging / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "artifact_digest": digest,
                "platform": _PLATFORM,
                "schema_digest": _SCHEMA,
                "interpreter": str(binary.relative_to(staging)),
                "cwd": str(site.relative_to(staging)),
                "files": files,
            },
            sort_keys=True,
        )
    )
    staging.rename(home / "releases" / digest)
    return ReleaseRef(
        artifact_digest=digest,
        manifest_digest=file_sha256(home / "releases" / digest / "manifest.json"),
        schema_digest=_SCHEMA,
        source_commit=_COMMIT,
    )


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed native tool, disposable exact label
        ["/bin/launchctl", *args], capture_output=True, text=True, timeout=30, check=False
    )


def _wait(condition: Callable[[], bool], what: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"native fixture: {what}")
        time.sleep(0.05)


@dataclass
class NativeHome:
    home: Path
    registry: Path
    label: str
    images: dict[str, ReleaseRef]
    births: list[OwnedProcess] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def run_dir(self) -> Path:
        return self.home / "run/ava-root"

    @property
    def target(self) -> str:
        return f"gui/{os.getuid()}/{self.label}"

    def image(self, tag: str) -> VerifiedRelease:
        return self.images[tag].verify(self.home, _PLATFORM)

    def helper(self) -> OwnedProcess:
        _reply, peer = client.ping_peer(sock_path=paths.permissions_helper_socket())
        helper = OwnedProcess.capture(psutil.Process(peer))
        self.note(helper)
        return helper

    def keeper(self) -> client.RootStatus:
        return client.root_status(sock_path=paths.permissions_helper_socket())

    def root(self) -> OwnedProcess:
        keeper = self.keeper()
        pid = keeper.get("pid")
        assert keeper["state"] == "running" and pid is not None, keeper
        root = OwnedProcess.capture(psutil.Process(pid))
        self.note(root)
        return root

    def note(self, birth: OwnedProcess) -> None:
        if birth not in self.births:
            self.births.append(birth)

    def environment(self) -> dict[str, str]:
        import pwd

        account = Path(pwd.getpwuid(os.getuid()).pw_dir)
        return dict(root_service.stage_environment(self.home, self.registry, account))

    def stage(self, tag: str) -> None:
        """Start one image's root directly (the steady state before an operation)."""
        image = self.image(tag)
        result = subprocess.run(  # noqa: S603 — fixture image interpreter, fixed argv
            list(image.module_argv("cli.release_transition.stage", "--operation", "setup")),
            cwd=image.cwd,
            env=self.environment(),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def select(self, tag: str) -> None:
        reference = self.images[tag]
        (self.home / "releases/current-release").write_text(
            json.dumps(
                {
                    "artifact_digest": reference.artifact_digest,
                    "manifest_digest": reference.manifest_digest,
                }
            )
        )


class NativeTransition(LocalTransition):
    """Real helper-root effects; only the application-level gates are absent.

    Quiescing and the single-home writer gate need the application database;
    ordinary stop's maintenance bookkeeping is replaced by its native root stop.
    """

    def __init__(self, request: Request) -> None:
        self.request = request
        self.home = Path(request.home)
        self.previous = request.previous.verify(self.home, _PLATFORM)
        self.candidate = request.candidate.verify(self.home, _PLATFORM)

    def preflight(self) -> None:
        return

    def quiesce(self) -> None:
        return

    def stop(self, operation: Operation) -> None:
        root_macos.verified_helper(operation)
        _root_driver._stop_root_service_tree(preserve=frozenset())
        _root_driver._require_root_absent()
        root_macos.require_stopped(operation)

    def start(self, journal: Journal) -> None:
        root_macos.start(journal, self.image(journal.operation))

    def observe(self, operation: Operation) -> None:
        root_macos.observe(operation, self.image(operation))
        root_macos.restore_boot(operation, self.image(operation))

    def resume(self, operation: Operation) -> None:
        root_macos.observe(operation, self.image(operation))


@pytest.fixture(scope="module")
def helper_app(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return native_fixture.build_helper_app(tmp_path_factory.mktemp("helper").resolve())


def _census(label: str) -> dict[str, Any]:
    listed = _launchctl("list").stdout
    return {
        "label_listed": label in listed,
        "fixture_labels": sorted(
            line.split()[-1] for line in listed.splitlines() if "release-start-fixture" in line
        ),
    }


def _cleanup(native: NativeHome) -> dict[str, Any]:
    report: dict[str, Any] = {}
    try:
        client.stop_root(sock_path=paths.permissions_helper_socket())
        # Graceful: the keeper's own TERM and reap, before the job goes away.
        _wait(lambda: native.keeper().get("pid") is None, "keeper root stopped", 15)
        report["keeper_stop"] = native.keeper()["state"]
    except (client.PermissionsHelperError, TimeoutError) as exc:
        report["keeper_stop"] = f"unavailable: {exc}"
    if _launchctl("print", native.target).returncode == 0:
        report["bootout"] = _launchctl("bootout", native.target).returncode
    _wait(lambda: _launchctl("print", native.target).returncode == 113, "label absent", 30)
    report["label"] = "absent"
    births: dict[str, str] = {}
    for birth in native.births:
        state = "closed"
        if birth.live():
            # Only a captured birth, re-identified by its native start time.
            birth.send_signal(signal.SIGKILL)
            _wait(lambda birth=birth: not birth.live(), f"birth {birth.pid} closed", 10)
            state = "killed-exact-birth"
        births[str(birth.pid)] = state
    report["births"] = births
    report["census"] = _census(native.label)
    assert not report["census"]["label_listed"]
    return report


@pytest.fixture
def native(
    helper_app: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[NativeHome]:
    # Darwin sockaddr_un holds 104 bytes; pytest's temporary paths are too long.
    base = Path(tempfile.mkdtemp(prefix="avrs-", dir="/tmp")).resolve()
    home = base / "home"
    (home / "run/ava-root").mkdir(parents=True)
    for directory in (home, home / "run", home / "run/ava-root"):
        directory.chmod(0o700)
    (home / "releases").mkdir(mode=0o700)
    registry = base / "clusters.json"
    registry.write_text("{}")
    label = f"com.ava.release-start-fixture.{uuid4().hex[:16]}"
    monkeypatch.setattr(settings.general, "ava_home", home)
    monkeypatch.setattr(settings.services, "permissions_helper_artifact_dir", helper_app.parent)
    if not native_fixture.stable_identity():
        monkeypatch.setattr(lifecycle, "_expected_dr", lambda: native_fixture.BUNDLE_REQUIREMENT)
    # The fixture home has no data plane; the bracket itself is unit-tested.
    monkeypatch.setattr(root_macos, "_data_plane", dict)
    native = NativeHome(home, registry, label, {tag: _image(home, tag) for tag in ("A", "B")})
    plist = base / f"{label}.plist"
    plist.write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": [str(helper_app / "Contents/MacOS/AvaPermissionsHelper")],
                "EnvironmentVariables": {
                    "AVA_PERMISSIONS_HELPER_SOCKET": str(paths.permissions_helper_socket()),
                    "AVA_PERMISSIONS_HELPER_ROOT_SEED": str(home / "run/ava-root/seed.json"),
                    "AVA_PERMISSIONS_HELPER_SKIP_REGISTRATION": "1",
                },
                "RunAtLoad": True,
                "KeepAlive": {"SuccessfulExit": False},
                "StandardOutPath": str(base / "helper.log"),
                "StandardErrorPath": str(base / "helper.log"),
            }
        )
    )
    native.evidence["census_before"] = _census(label)
    assert not native.evidence["census_before"]["label_listed"]
    try:
        result = _launchctl("bootstrap", f"gui/{os.getuid()}", str(plist))
        assert result.returncode == 0, result.stderr
        _wait(lambda: paths.permissions_helper_socket().exists(), "helper socket")
        native.evidence["helper"] = native.helper().pid
        native.select("A")
        native.stage("A")
        native.evidence["initial_root"] = native.root().pid
        yield native
    finally:
        native.evidence["operations"] = [
            {key: record[key] for key in ("phase", "direction", "attempt", "root", "error")}
            for record in (
                json.loads(path.read_text())
                for path in sorted(home.glob("updates/*/operation.json"))
            )
        ]
        native.evidence["cleanup"] = _cleanup(native)
        target = os.environ.get("AVA_NATIVE_EVIDENCE_DIR")
        proof = json.dumps(native.evidence, indent=2, default=str) + "\n"
        (tmp_path / "native-proof.json").write_text(proof)
        if target:
            Path(target, f"{label}.json").write_text(proof)
        shutil.rmtree(base)


def _operation(native: NativeHome, previous: str, candidate: str) -> Request:
    request = Request(
        id=uuid4(),
        home=str(native.home),
        registry=str(native.registry),
        created_at=datetime.now(UTC),
        platform_tag=_PLATFORM,
        machine="fixture",
        previous=native.images[previous],
        candidate=native.images[candidate],
        executor=native.images[candidate],
        configuration_digest="f" * 64,
    )
    current = journal.create(request)
    # The persistent helper is real; this process stands in for the finite job.
    plan = macos.plan_launch(request.path, request.executor.verify(native.home, _PLATFORM))
    with journal.exclusive(request.path) as locked:
        assert locked.operation == current
        locked.record_launch(plan)
        locked.mark_launch_attempted()
    return request


def _drive(request: Request) -> Operation:
    with journal.exclusive(request.path) as current:
        drive(current, NativeTransition(request))
    final = journal.read_operation(request.path)
    if final.terminal:
        _retire_stand_in(request)
    return final


def _retire_stand_in(request: Request) -> None:
    """This process stood in for the finite job: its launch never reached launchd.

    The next operation requires the previous executor's recorded retirement;
    the stand-in's terminal is its planned label, never loaded, with no births.
    """
    with journal.exclusive(request.path) as current:
        assert current.operation.launch is not None
        launch = macos.DarwinLaunch.model_validate(current.operation.launch)
        assert _launchctl("print", launch.target).returncode == 113, "stand-in label loaded"
        terminal = macos.DarwinJob(
            label=launch.label,
            domain=launch.domain,
            boot_id=launch.boot_id,
            evidence="launchd",
            asid=None,
            state="not running",
            runs=1,
            helper=None,
            executor=None,
            pgid=None,
            exit_code=0,
            signal=None,
            closed=None,
        )
        current.request_retirement(terminal.model_dump(mode="json"))
        current.record_retired()


def _require_steady(native: NativeHome, tag: str, helper: OwnedProcess) -> dict[str, Any]:
    """Root is the helper's kept direct child in its own session, pinned to one image."""
    image = native.image(tag)
    root = native.root()
    process = psutil.Process(root.pid)
    keeper = native.keeper()
    persisted = json.loads((native.run_dir / "seed.json").read_text())
    prefix = list(image.module_argv("services.ava_root"))
    assert process.ppid() == helper.pid and helper.live()
    assert os.getsid(root.pid) == root.pid == os.getpgid(root.pid)
    assert process.cmdline()[: len(prefix)] == prefix and process.cwd() == str(image.cwd)
    assert keeper.get("seed", {}).get("argv") == persisted["argv"] == process.cmdline()
    assert keeper["stop_requested"] is False
    selected = json.loads((native.home / "releases/current-release").read_text())
    assert selected["artifact_digest"] == image.digest
    return {"image": tag, "root": root.pid, "helper": helper.pid, "restarts": keeper["restarts"]}


def test_release_a_to_b_to_a_through_the_persistent_home_helper(native: NativeHome) -> None:
    helper = native.helper()
    first = native.root()
    forward = _drive(_operation(native, "A", "B"))
    assert (forward.phase, forward.direction) == ("complete", "candidate")
    assert not first.live(), "the previous image's root survived its release"
    native.evidence["forward"] = _require_steady(native, "B", helper)
    middle = native.root()
    back = _drive(_operation(native, "B", "A"))
    assert (back.phase, back.direction) == ("complete", "candidate")
    assert not middle.live()
    native.evidence["back"] = _require_steady(native, "A", helper)
    # Neither lifetime ended the other: one helper birth owned all three roots.
    assert native.helper() == helper


def test_candidate_start_failure_starts_the_previous_image(native: NativeHome) -> None:
    helper = native.helper()
    first = native.root()
    (native.run_dir / f"fixture-stage-fail-{'B'}").touch()
    request = _operation(native, "A", "B")
    final = _drive(request)
    assert (final.phase, final.direction) == ("complete", "previous")
    assert not first.live(), "recovery starts a new previous-image root after stop"
    native.evidence["recovered"] = _require_steady(native, "A", helper)
    custody = RootCustody.model_validate(final.root)
    assert custody.direction == "previous" and custody.root is not None


def test_keeper_restart_of_the_candidate_is_detected_and_recovered(
    native: NativeHome, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The keeper silently respawns a crashed candidate; the journaled birth refuses it."""
    helper = native.helper()
    real = root_macos.observe_selected
    killed: list[int] = []

    def crash_then_observe(operation: Operation, image: VerifiedRelease) -> None:
        if operation.direction == "candidate" and not killed:
            root = native.root()
            killed.append(root.pid)
            assert root.send_signal(signal.SIGKILL)
            _wait(lambda: native.keeper().get("pid") not in {None, root.pid}, "keeper restart")
            replacement = native.root()
            _wait(lambda: native.keeper()["restarts"] == 1, "restart counted")
            # Let the respawned root serve first: a root that holds its lock but
            # has not bound its control socket yet makes ordinary stop refuse
            # (unknown custody), which is a different, fail-closed outcome.
            _wait(lambda: _serving(replacement), "respawned root serving")
        real(operation, image)

    monkeypatch.setattr(root_macos, "observe_selected", crash_then_observe)
    final = _drive(_operation(native, "A", "B"))
    assert (final.phase, final.direction) == ("complete", "previous") and killed
    native.evidence["restart_detected"] = _require_steady(native, "A", helper)


def test_stage_killed_mid_start_recovers_to_the_previous_image(
    native: NativeHome, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bounded start action is killed after root_seed; recovery stops B and starts A."""
    helper = native.helper()
    (native.run_dir / "fixture-stage-hang-B").touch()
    monkeypatch.setattr(root_macos, "START_TIMEOUT_S", 8)
    final = _drive(_operation(native, "A", "B"))
    assert (final.phase, final.direction) == ("complete", "previous")
    native.evidence["stage_killed"] = _require_steady(native, "A", helper)


class ControllerLost(BaseException):
    """A process death cannot run the executor's exception handling."""


def test_executor_loss_after_the_effect_resumes_with_the_same_root(
    native: NativeHome, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = native.helper()
    real = root_macos._require_same_data_plane
    lost: list[int] = []

    def die_once(before: dict[str, OwnedProcess], after: dict[str, OwnedProcess]) -> None:
        real(before, after)
        if not lost:
            lost.append(native.root().pid)
            raise ControllerLost

    monkeypatch.setattr(root_macos, "_require_same_data_plane", die_once)
    request = _operation(native, "A", "B")
    with pytest.raises(ControllerLost):
        _drive(request)
    interrupted = journal.read_operation(request.path)
    intent = RootCustody.model_validate(interrupted.root)
    assert (interrupted.phase, intent.direction, intent.root) == ("starting", "candidate", None)
    final = _drive(request)
    assert (final.phase, final.direction) == ("complete", "candidate")
    custody = RootCustody.model_validate(final.root)
    assert custody.root is not None and custody.root.pid == lost[0]
    native.evidence["resumed"] = _require_steady(native, "B", helper)


def test_helper_killed_mid_transition_is_an_explicit_refusal(
    native: NativeHome, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The keeper's root is orphaned; nothing adopts or signals it, the operation holds."""
    helper = native.helper()
    real = root_macos.observe_selected
    orphan: list[OwnedProcess] = []

    def kill_helper(operation: Operation, image: VerifiedRelease) -> None:
        if not orphan:
            orphan.append(native.root())
            assert helper.send_signal(signal.SIGKILL)
            _wait(lambda: not helper.live(), "helper death")
            # launchd restarts the helper (KeepAlive); the new keeper sees a held lock.
            _wait(lambda: _answers_as_new(native, helper), "helper restart", 40)
        real(operation, image)

    monkeypatch.setattr(root_macos, "observe_selected", kill_helper)
    request = _operation(native, "A", "B")
    with pytest.raises(RuntimeError, match="signed-helper custody"):
        _drive(request)
    held = journal.read_operation(request.path)
    assert (held.phase, held.direction) == ("stopping", "previous") and held.error
    assert orphan[0].live() and psutil.Process(orphan[0].pid).ppid() == 1
    replacement = native.helper()
    keeper = native.keeper()
    assert keeper["state"] == "conflict" and keeper.get("pid") is None, keeper
    native.evidence["helper_killed"] = {
        "orphan": orphan[0].pid,
        "replacement_helper": replacement.pid,
        "keeper": keeper["state"],
        "held": {"phase": held.phase, "direction": held.direction, "error": held.error},
    }


def _serving(root: OwnedProcess) -> bool:
    from shared.root_control.client import RootClientError, root_process

    try:
        return root_process() == root
    except RootClientError:
        return False


def _answers_as_new(native: NativeHome, old: OwnedProcess) -> bool:
    try:
        return native.helper().pid != old.pid
    except (client.PermissionsHelperError, OSError, psutil.Error):
        return False
