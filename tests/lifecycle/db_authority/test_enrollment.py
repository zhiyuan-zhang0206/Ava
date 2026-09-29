"""The unit enrollment secret: gateway store, rotation, revocation, coordinator channel.

A unit is enrolled when it first receives a bundle (its join or the one-time
cutover). Only explicit operator commands change the gateway record. The
secret keys the release coordinator channel: requests authenticate by HMAC
against the gateway's CURRENT record (so rotation and revocation take effect
at once), replays and skewed clocks refuse, and sealed payloads open only for
the unit and operation they were sealed for. No database is needed.
"""

from __future__ import annotations

import dataclasses
import secrets
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from base.cluster.authority import channel, unit
from cli.commands.cluster import control as cluster_cmd

_ENDPOINT = "postgresql://ava@10.0.0.7:6433/ava"
_MACHINE = "mini"


@pytest.fixture
def gateway(tmp_path: Path, seed_write_generation: Callable[[Path], Any]) -> Path:
    home = (tmp_path / "gateway").resolve()
    home.mkdir(mode=0o700)
    seed_write_generation(home)
    return home


@pytest.fixture
def runner_home(tmp_path: Path) -> Path:
    home = (tmp_path / "runner").resolve()
    home.mkdir(mode=0o700)
    return home


@pytest.fixture
def identity(runner_home: Path) -> unit.UnitIdentity:
    return unit.UnitIdentity(machine=_MACHINE, home=str(runner_home))


def _bundle(gateway: Path, identity: unit.UnitIdentity) -> unit.Bundle:
    issued = unit.issue_bundle(
        gateway, unit=identity, endpoint=_ENDPOINT, cluster_secret="", ttl_s=600
    )
    return unit.open_bundle(issued.envelope, issued.transport_key)


# ── gateway store ───────────────────────────────────────────────────────────


def test_rotation_replaces_the_secret_and_the_next_bundle_delivers_it(
    gateway: Path, identity: unit.UnitIdentity, runner_home: Path
) -> None:
    first = _bundle(gateway, identity).enrollment
    rotated = unit.rotate_enrollment(gateway, identity)
    assert rotated.unit == identity
    assert rotated.enrollment_id != first.enrollment_id and rotated.secret != first.secret
    record = unit.enrollment_record_path(gateway, identity)
    assert record.stat().st_mode & 0o777 == 0o600
    assert unit.load_enrollment(gateway, identity) == rotated
    delivered = _bundle(gateway, identity)
    assert delivered.enrollment == rotated
    unit.install_bundle(
        runner_home,
        delivered,
        machine=_MACHINE,
        served_endpoint=_ENDPOINT,
        probe=lambda _dsn: None,
    )
    assert unit.load_unit_enrollment(runner_home) == rotated


def test_revocation_deletes_the_record_and_reissue_re_enrolls(
    gateway: Path, identity: unit.UnitIdentity
) -> None:
    enrolled = _bundle(gateway, identity).enrollment
    assert unit.revoke_enrollment(gateway, identity) == enrolled
    assert not unit.enrollment_record_path(gateway, identity).exists()
    assert unit.load_enrollment(gateway, identity) is None
    reenrolled = _bundle(gateway, identity).enrollment
    assert reenrolled.enrollment_id != enrolled.enrollment_id


def test_an_unenrolled_unit_has_nothing_to_rotate_or_revoke(
    gateway: Path, identity: unit.UnitIdentity
) -> None:
    for change in (unit.rotate_enrollment, unit.revoke_enrollment):
        with pytest.raises(unit.UnitCapabilityError, match="holds no enrollment"):
            change(gateway, identity)
    assert not unit.enrollment_record_path(gateway, identity).exists()


def test_the_unit_reads_only_its_own_installed_enrollment(
    gateway: Path, identity: unit.UnitIdentity, runner_home: Path, tmp_path: Path
) -> None:
    assert unit.load_unit_enrollment(runner_home) is None
    bundle = _bundle(gateway, identity)
    unit.install_bundle(
        runner_home, bundle, machine=_MACHINE, served_endpoint=_ENDPOINT, probe=lambda _d: None
    )
    moved = (tmp_path / "moved").resolve()
    unit.unit_enrollment_path(runner_home).parent.rename(moved)
    moved.parent.joinpath("elsewhere").mkdir(mode=0o700)
    target = moved.parent / "elsewhere" / "db-authority"
    moved.rename(target)
    with pytest.raises(unit.UnitCapabilityError, match="belongs to another home"):
        unit.load_unit_enrollment(target.parent)


# ── operator commands ───────────────────────────────────────────────────────


@pytest.fixture
def on_gateway(gateway: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    def this_gateway(_verb: str) -> Path:
        return gateway

    monkeypatch.setattr(cluster_cmd, "_gateway_authority_home", this_gateway)
    return gateway


def test_rotate_and_revoke_commands_print_ids_never_secrets(
    on_gateway: Path, identity: unit.UnitIdentity, capsys: pytest.CaptureFixture[str]
) -> None:
    first = unit.ensure_enrollment(on_gateway, identity)
    assert cluster_cmd.cmd_db_authority_rotate_enrollment(machine=_MACHINE, home=identity.home) == 0
    rotated = unit.load_enrollment(on_gateway, identity)
    assert rotated is not None
    printed = capsys.readouterr().out
    assert rotated.enrollment_id in printed and "issue-unit" in printed
    assert rotated.secret not in printed and first.secret not in printed
    assert cluster_cmd.cmd_db_authority_revoke_enrollment(machine=_MACHINE, home=identity.home) == 0
    printed = capsys.readouterr().out
    assert rotated.enrollment_id in printed and rotated.secret not in printed
    # Revocation cuts the coordinator channel only; the operator is told so.
    assert "stay valid until the generation rotates" in printed
    assert unit.load_enrollment(on_gateway, identity) is None
    assert cluster_cmd.cmd_db_authority_revoke_enrollment(machine=_MACHINE, home=identity.home) == 1
    assert "holds no enrollment" in capsys.readouterr().err


def test_enrollment_commands_refuse_while_a_release_operation_is_incomplete(
    gateway: Path,
    identity: unit.UnitIdentity,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The operation captured the units it releases; their identity stays put."""

    def incomplete(_home: Path) -> None:
        raise RuntimeError("a release operation is incomplete")

    enrolled = unit.ensure_enrollment(gateway, identity)
    monkeypatch.setattr("base.host.env.bootstrap.config_source_is_local", lambda: True)
    monkeypatch.setattr("base.paths.ava_home", lambda: gateway)
    monkeypatch.setattr(
        "base.deploy.release.operation.require_configuration_write_authorized", incomplete
    )
    for command in (
        cluster_cmd.cmd_db_authority_rotate_enrollment,
        cluster_cmd.cmd_db_authority_revoke_enrollment,
    ):
        assert command(machine=_MACHINE, home=identity.home) == 1
        assert "a release operation is incomplete" in capsys.readouterr().err
    assert unit.load_enrollment(gateway, identity) == enrolled


# ── coordinator channel ─────────────────────────────────────────────────────


@pytest.fixture
def enrolled(gateway: Path, identity: unit.UnitIdentity) -> unit.Enrollment:
    return unit.ensure_enrollment(gateway, identity)


_REQUEST: dict[str, Any] = {"method": "POST", "path": "/v1/op/x/unit/y/report", "body": b"{}"}


def _sign(enrollment: unit.Enrollment, operation: str, **changes: Any) -> channel.RequestProof:
    return channel.sign_request(enrollment, operation=operation, **(_REQUEST | changes))


def _verify(
    enrollment: unit.Enrollment,
    proof: channel.RequestProof,
    window: channel.ReplayWindow,
    **changes: Any,
) -> None:
    channel.verify_request(enrollment, proof, window=window, **(_REQUEST | changes))


def test_a_signed_request_verifies_once(enrolled: unit.Enrollment) -> None:
    operation = str(uuid4())
    window = channel.ReplayWindow(operation)
    proof = _sign(enrolled, operation)
    _verify(enrolled, proof, window)
    with pytest.raises(channel.ChannelRefusedError, match="replays an admitted nonce"):
        _verify(enrolled, proof, window)


@pytest.mark.parametrize(
    "change",
    [
        {"method": "GET"},
        {"path": "/v1/op/x/unit/y/capability"},
        {"body": b'{"phase": "ready"}'},
    ],
)
def test_a_request_changed_after_signing_refuses(
    enrolled: unit.Enrollment, change: dict[str, Any]
) -> None:
    operation = str(uuid4())
    with pytest.raises(channel.ChannelRefusedError, match="does not authenticate"):
        _verify(enrolled, _sign(enrolled, operation), channel.ReplayWindow(operation), **change)


def test_a_request_for_another_operation_or_with_changed_proof_fields_refuses(
    enrolled: unit.Enrollment,
) -> None:
    operation = str(uuid4())
    proof = _sign(enrolled, operation)
    for window, forged in (
        (channel.ReplayWindow(str(uuid4())), proof),
        (
            channel.ReplayWindow(operation),
            dataclasses.replace(proof, timestamp=proof.timestamp + 1),
        ),
        (channel.ReplayWindow(operation), dataclasses.replace(proof, nonce="0" * 32)),
    ):
        with pytest.raises(channel.ChannelRefusedError, match="does not authenticate"):
            _verify(enrolled, forged, window)


def test_a_forged_request_does_not_burn_the_nonce(enrolled: unit.Enrollment) -> None:
    operation = str(uuid4())
    window = channel.ReplayWindow(operation)
    proof = _sign(enrolled, operation)
    with pytest.raises(channel.ChannelRefusedError, match="does not authenticate"):
        _verify(enrolled, dataclasses.replace(proof, signature="0" * 64), window)
    _verify(enrolled, proof, window)


@pytest.fixture
def eager_switching() -> Iterator[None]:
    """Switch threads every microsecond, so a check-then-act race shows at once."""
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        yield
    finally:
        sys.setswitchinterval(interval)


def _proof(timestamp: int, nonce: str | None = None) -> channel.RequestProof:
    return channel.RequestProof("a" * 32, timestamp, nonce or secrets.token_hex(16), "b" * 64)


def _race(window: channel.ReplayWindow, proofs: list[channel.RequestProof], now: int) -> list[str]:
    """Admit `proofs` from one thread each, released together; each outcome."""
    start = threading.Barrier(len(proofs))
    outcomes: list[str] = []

    def admit(proof: channel.RequestProof) -> None:
        start.wait()
        try:
            window.admit("unit", proof, now)
        except channel.ChannelRefusedError:
            outcomes.append("replay")
        except Exception as exc:  # any other exception is the finding: record its type
            outcomes.append(type(exc).__name__)
        else:
            outcomes.append("admitted")

    threads = [threading.Thread(target=admit, args=(proof,)) for proof in proofs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes


@pytest.mark.usefixtures("eager_switching")
def test_handler_threads_share_one_window_without_losing_a_request() -> None:
    """Concurrent admissions over stale entries each prune and admit, none raises."""
    stale, now = 1_000_000, 1_000_000 + channel.MAX_SKEW_S + 1
    for _ in range(100):
        window = channel.ReplayWindow("op")
        for _ in range(300):
            window.admit("unit", _proof(stale), stale)
        assert _race(window, [_proof(now) for _ in range(4)], now) == ["admitted"] * 4


class _SlowMembership(dict[tuple[str, str], int]):
    """A replay record whose membership check yields before it answers, so every
    racing admission runs between another's check and its insert."""

    def __contains__(self, key: object) -> bool:
        found = super().__contains__(key)
        time.sleep(0.05)
        return found


def test_one_nonce_is_admitted_once_however_many_threads_race() -> None:
    now = 1_000_000
    window = channel.ReplayWindow("op")
    window._seen = _SlowMembership()
    replayed = _proof(now)
    outcomes = _race(window, [replayed] * 4, now)
    assert sorted(outcomes) == ["admitted", "replay", "replay", "replay"]


def test_a_request_outside_the_clock_skew_refuses(enrolled: unit.Enrollment) -> None:
    operation = str(uuid4())
    proof = channel.sign_request(enrolled, operation=operation, now=1_000_000.0, **_REQUEST)
    with pytest.raises(channel.ChannelRefusedError, match="clock skew"):
        channel.verify_request(
            enrolled,
            proof,
            window=channel.ReplayWindow(operation),
            now=1_000_000.0 + channel.MAX_SKEW_S + 1,
            **_REQUEST,
        )


def test_rotation_and_another_unit_stop_authenticating_at_once(
    gateway: Path, identity: unit.UnitIdentity, enrolled: unit.Enrollment, tmp_path: Path
) -> None:
    operation = str(uuid4())
    stale = _sign(enrolled, operation)
    rotated = unit.rotate_enrollment(gateway, identity)
    with pytest.raises(channel.ChannelRefusedError, match="another enrollment"):
        _verify(rotated, stale, channel.ReplayWindow(operation))
    other = unit.ensure_enrollment(
        gateway, unit.UnitIdentity(machine="other", home=str(tmp_path / "other"))
    )
    impostor = dataclasses.replace(_sign(other, operation), enrollment_id=rotated.enrollment_id)
    with pytest.raises(channel.ChannelRefusedError, match="does not authenticate"):
        _verify(rotated, impostor, channel.ReplayWindow(operation))


def test_a_sealed_payload_opens_only_for_its_unit_and_operation(
    gateway: Path, identity: unit.UnitIdentity, enrolled: unit.Enrollment, tmp_path: Path
) -> None:
    operation = str(uuid4())
    sealed = channel.seal(enrolled, operation=operation, plaintext=b"capability")
    assert b"capability" not in sealed
    assert channel.open_sealed(enrolled, operation=operation, sealed=sealed) == b"capability"
    other = unit.ensure_enrollment(
        gateway, unit.UnitIdentity(machine="other", home=str(tmp_path / "other"))
    )
    flipped = sealed[:-1] + bytes([sealed[-1] ^ 1])
    for enrollment, op, payload in (
        (enrolled, str(uuid4()), sealed),
        (other, operation, sealed),
        (enrolled, operation, flipped),
        (unit.rotate_enrollment(gateway, identity), operation, sealed),
    ):
        with pytest.raises(channel.ChannelRefusedError, match="does not open"):
            channel.open_sealed(enrollment, operation=op, sealed=payload)
    with pytest.raises(channel.ChannelRefusedError, match="truncated"):
        channel.open_sealed(enrolled, operation=operation, sealed=sealed[:5])


def test_a_malformed_proof_is_refused_before_verification() -> None:
    with pytest.raises(channel.ChannelRefusedError, match="malformed signature"):
        channel.RequestProof("a" * 32, 0, "b" * 32, "not-hex")
    with pytest.raises(channel.ChannelRefusedError, match="malformed enrollment or nonce"):
        channel.RequestProof("A" * 32, 0, "b" * 32, "c" * 64)
