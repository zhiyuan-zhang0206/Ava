"""Reading back what the last updater session did — and refusing to answer when the
log does not speak for *this* update.

The orchestrator's `POLL_STALLED` covers two hosts with opposite next actions: one
whose preflight refused (untouched, still serving its old code) and one whose updater
died after moving the checkout. The exit code that separates them never left the box.
These cover the reader that lets it: what it can classify on each platform, and —
more important — the cases where it must return None rather than hand back a previous
update's verdict as this one's.
"""

from __future__ import annotations

import os
import time
from datetime import UTC
from pathlib import Path

import pytest

from ops import updater_outcome as uo

# A real POSIX decline, as prod writes it (issue #995's own transcript).
_DECLINED_LOG = """\
[updater] force-checkout to abc1234
  ✗ gateway unreachable at http://100.64.0.2:8000: [Errno 61] Connection refused.
  ✗ refusing self-update: preflight probes failed — host still serving
[updater] restart DECLINED by its own preflight — nothing was stopped and this host is still serving
[session-exit] rc=3
"""

_FAILED_LOG = """\
[updater] force-checkout to abc1234
[updater] restart FAILED (rc=1) after stopping — recovering with ava start
[session-exit] rc=1
"""

# The Windows shape a host still on the pre-verdict chain writes — the supervisor's
# own appended log, decline sentence, no `[session-exit]` line. Real for exactly one
# rollout: the one that ships the ladder's verdicts, where the old chain is what
# spawns before the checkout moves.
_WINDOWS_DECLINED_LOG = """\
[updater] force-checkout to "abc1234"
[updater] restart DECLINED by its own preflight -- host still serving, not starting over it
"""

# The Windows shape now: same appended log, plus the literal rc the ladder's abort
# branch states (`ops._update_shell`, `ops.cluster_deploy`).
_WINDOWS_ABORTED_LOG = """\
[updater] force-checkout to "abc1234"
[updater] checkout/sync or tree verification FAILED -- refusing to start services on a possibly-mixed tree; the host stays on its current code
[session-exit] rc=1
"""


class _Backend:
    """A session backend stub. `log` is the path it claims to redirect output to —
    None is the legacy shape (a pane, not a file)."""

    def __init__(self, log: Path | None = None) -> None:
        self.log = log

    def session_log_path(self, name: str) -> Path | None:
        return self.log


# The posture row `last_updater_outcome` reads. One module-level slot per worker
# (the `home` fixture resets it and installs the read stub); the anchor is the
# row's `paused_at` — the pause moment the retired `cluster_paused` file's mtime
# used to carry (R1 old-signal sweep, PR5).
_PAUSE_STATE: dict[str, object] = {"state": None}


def _paused_row(*, age_s: float = 30.0, posture: str = "paused") -> None:
    from datetime import datetime, timedelta

    from shared.host_deploy_state import HostDeployState

    now = datetime.now(UTC)
    _PAUSE_STATE["state"] = HostDeployState(
        machine="test",
        posture=posture,
        updated_at=now,
        updater_lease_expires_at=None,
        paused_at=now - timedelta(seconds=age_s),
    )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated `$AVA_HOME` with a logs dir, a paused posture-row stub (the
    pause anchor `last_updater_outcome` reads), and the legacy backend shape (no
    supervisor log) unless a test overrides it."""
    monkeypatch.setattr("shared.paths.ava_home", lambda: tmp_path)
    monkeypatch.setattr("shared.session_backend.get_backend", _Backend)
    _PAUSE_STATE["state"] = None
    monkeypatch.setattr("shared.host_deploy_state.read", lambda *_a, **_k: _PAUSE_STATE["state"])  # pyright: ignore[reportUnknownArgumentType]
    (tmp_path / "logs").mkdir()
    return tmp_path


def _paused(home: Path, *, age_s: float = 30.0) -> None:
    _paused_row(age_s=age_s)


def _write_log(path: Path, text: str, *, age_s: float = 0.0) -> Path:
    path.write_text(text, encoding="utf-8")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path


def test_a_declined_preflight_is_reported_as_a_refusal_with_its_reason(home: Path) -> None:
    """The case the whole thing exists for: nothing was stopped, the host is intact,
    and the reason (an unreachable gateway) is already in the log — it just never
    travelled."""
    _paused(home)
    _write_log(home / "logs" / "updater-1785470000.log", _DECLINED_LOG)

    outcome = uo.last_updater_outcome()

    assert outcome is not None
    assert outcome.kind == "declined"
    assert outcome.rc == 3
    assert "gateway unreachable at http://100.64.0.2:8000" in outcome.detail
    assert "still serving" in uo.describe_updater_outcome(outcome)


def test_a_failed_restart_is_reported_as_an_exit_code(home: Path) -> None:
    """The other half: the stop already happened, so this host may be half
    transitioned — a different next action from the refusal above."""
    _paused(home)
    _write_log(home / "logs" / "updater-1785470000.log", _FAILED_LOG)

    outcome = uo.last_updater_outcome()

    assert outcome is not None
    assert (outcome.kind, outcome.rc) == ("exited", 1)
    assert "rc=1" in uo.describe_updater_outcome(outcome)


def test_the_windows_log_still_carries_the_refusal(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows writes no `updater-<epoch>.log` at all — the supervisor owns the
    redirect and appends to its own file. A host still running the pre-verdict chain
    (the rollout that ships the verdicts spawns the OLD one) emits no `[session-exit]`
    line; the decline marker the ladder has always echoed still carries the fact that
    matters most, and `rc` stays honestly None rather than invented."""
    out_log = home / "logs" / "ava-updater.out.log"
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: _Backend(out_log))
    _paused(home)
    _write_log(out_log, _WINDOWS_DECLINED_LOG)
    assert not list((home / "logs").glob("updater-*.log"))  # exactly the Windows shape

    outcome = uo.last_updater_outcome()

    assert outcome is not None
    assert outcome.kind == "declined"
    assert outcome.rc is None


def test_the_windows_ladder_now_states_a_verdict_the_reader_can_classify(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap this closes: a Windows abort — the cheapest failure the chain has, a
    `git fetch` that could not reach origin — used to read `unknown`, the same label
    as an updater killed mid-flight, and the operator had to ssh into the one box
    that is hardest to ssh into to tell them apart. The ladder states the rc
    literally now, so the reader classifies it like any POSIX run."""
    out_log = home / "logs" / "ava-updater.out.log"
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: _Backend(out_log))
    _paused(home)
    _write_log(out_log, _WINDOWS_ABORTED_LOG)

    outcome = uo.last_updater_outcome()

    assert outcome is not None
    assert (outcome.kind, outcome.rc) == ("exited", 1)
    assert "rc=1" in uo.describe_updater_outcome(outcome)


def test_the_emitted_exit_line_is_the_one_the_reader_parses(home: Path) -> None:
    """Emitter and parser have to agree on more than the words: a marker printed in a
    shape `_classify` does not recognise is a marker nothing reads, and that failure is
    silent — it looks exactly like the missing verdict this replaced."""
    _paused(home)
    _write_log(home / "logs" / "updater-1785470000.log", uo.native_exit_line(7) + "\n")

    outcome = uo.last_updater_outcome()

    assert outcome is not None
    assert (outcome.kind, outcome.rc) == ("exited", 7)


def test_only_a_written_ending_counts_as_the_updater_having_finished() -> None:
    """Phase B's second stop-proof. `unknown` is the one that must NOT qualify: it is
    what a still-running updater's log looks like, so treating it as an ending would
    abandon a host in the middle of its work — the opposite of the mistake this fixes,
    and the expensive direction."""
    assert uo.updater_outcome_terminal(uo.UpdaterOutcome(kind="exited", rc=0)) is True
    assert uo.updater_outcome_terminal(uo.UpdaterOutcome(kind="declined")) is True
    assert uo.updater_outcome_terminal(uo.UpdaterOutcome(kind="unknown")) is False
    assert uo.updater_outcome_terminal(None) is False


def test_a_refusal_is_rendered_with_the_operators_next_step(home: Path) -> None:
    """A refusal is the one outcome whose host is fine, so the verdict alone leaves
    the operator looking for damage there is none of. The rendering names the thing to
    act on instead — the preflight's complaint, which is gateway reachability in the
    shape #995 was filed for."""
    _paused(home)
    _write_log(home / "logs" / "updater-1785470000.log", _DECLINED_LOG)

    described = uo.describe_updater_outcome(uo.last_updater_outcome())

    assert "gateway reachability" in described
    assert "re-run the update" in described


def test_a_posix_decline_exit_code_is_a_refusal_too(home: Path) -> None:
    """`updater_outcome_declined` is asked at the call sites that choose the next
    step, so it must answer for both spellings of the same fact: the marker (Windows'
    only signal) and the POSIX exit line carrying rc=3."""
    assert uo.updater_outcome_declined(uo.UpdaterOutcome(kind="declined")) is True
    assert uo.updater_outcome_declined(uo.UpdaterOutcome(kind="exited", rc=3)) is True
    assert uo.updater_outcome_declined(uo.UpdaterOutcome(kind="exited", rc=1)) is False
    assert uo.updater_outcome_declined(uo.UpdaterOutcome(kind="unknown")) is False
    assert uo.updater_outcome_declined(None) is False


def test_an_rc3_exit_also_carries_the_next_step(home: Path) -> None:
    """The `exited` branch renders rc=3 as a refusal as well, and must not stop at
    noting it — a host told it is 'still serving' with no action named is the same
    dead end the plain verdict was."""
    described = uo.describe_updater_outcome(uo.UpdaterOutcome(kind="exited", rc=3))

    assert "still serving" in described
    assert "gateway reachability" in described


def test_a_log_with_neither_marker_is_unknown_not_a_clean_exit(home: Path) -> None:
    """An updater killed mid-flight leaves whatever it had written. That is not a
    verdict, and must not be rendered as one — 'no exit verdict' is the answer."""
    _paused(home)
    _write_log(home / "logs" / "updater-1785470000.log", "[updater] force-checkout to abc1234\n")

    outcome = uo.last_updater_outcome()

    assert outcome is not None
    assert (outcome.kind, outcome.rc) == ("unknown", None)
    assert "no exit verdict" in uo.describe_updater_outcome(outcome)


# ─── the staleness anchor: a previous update's log is not this update's outcome ──


def test_a_log_older_than_the_pause_is_not_this_updates_outcome(home: Path) -> None:
    """The log directory always holds a previous run's log, and on Windows the
    supervisor appends every run to ONE file — so "the newest updater log" is an
    answer even when this update never wrote a line. A stale `rc=0` read as this
    update's outcome is strictly worse than no answer, so the read is anchored to the
    pause moment `pause_local_cluster()` stamps (`host_deploy_state.paused_at`)
    immediately before the log is opened.
    """
    _paused(home, age_s=30.0)
    _write_log(home / "logs" / "updater-1785000000.log", _FAILED_LOG, age_s=3600.0)

    assert uo.last_updater_outcome() is None
    assert uo.describe_updater_outcome(None) == "no updater record for this update"


def test_an_unpaused_host_has_no_update_to_be_the_outcome_of(home: Path) -> None:
    """No pause flag means no update in flight here, so even a fresh log describes a
    finished one. The consumer (a stalled host) is paused by definition."""
    _write_log(home / "logs" / "updater-1785470000.log", _DECLINED_LOG)

    assert uo.last_updater_outcome() is None


def test_no_log_at_all_is_no_record(home: Path) -> None:
    _paused(home)

    assert uo.last_updater_outcome() is None


def test_the_read_never_raises_into_the_status_probe(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This runs inside `status_snapshot`, which answers the probe the entire rollout
    poll depends on. A parse failure that 500'd the status endpoint would turn a
    diagnosable stall into an unreachable host — strictly worse than the gap it was
    added to close."""

    def _boom(_session: str) -> Path:
        raise RuntimeError("log directory unreadable")

    _paused(home)
    monkeypatch.setattr(uo, "_newest_log", _boom)

    assert uo.last_updater_outcome() is None


# ─── the run anchor: on a log holding every run, which lines are THIS run's ──────


@pytest.fixture
def windows_log(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The Windows shape: no `updater-<epoch>.log` at all, and one supervisor-owned
    file every run appends to. Paused 30s ago, so a marker stamped `now` is this
    update's and one stamped an hour ago is not."""
    out_log = home / "logs" / "ava-updater.out.log"
    monkeypatch.setattr("shared.session_backend.get_backend", lambda: _Backend(out_log))
    _paused(home, age_s=30.0)
    return out_log


def _marker(*, age_s: float = 0.0) -> str:
    """A run marker as the updater echoes it, `age_s` seconds ago.

    Spelled out here rather than built from the module under test. This is a format
    written by one commit's updater and read by another's — for the whole length of a
    rollout — so a test that re-derived it would stay green through a change that
    silently stops anchoring anything.
    `test_the_line_the_updater_echoes_is_the_line_the_reader_anchors_on` is what ties
    this literal back to what the command line actually prints.
    """
    return f"[updater-run] {int(time.time() - age_s)}"


def test_a_previous_runs_decline_is_not_this_runs_verdict(windows_log: Path) -> None:
    """Issue #1117, the whole point.

    The supervisor appends every run to one file, so the pause anchor — which dates
    the FILE — leaves a bounded tail able to open in the middle of an earlier run. A
    decline marker there used to be read as this run's, reporting a host as untouched
    and still serving its old code when a newer run may have half-transitioned it:
    the two readings `POLL_STALLED` covers, and the operator is handed the wrong one.

    This run wrote its own start marker first, so the slice begins after it and the
    earlier run's refusal is simply not in what gets classified.
    """
    _write_log(
        windows_log,
        _marker(age_s=3600.0)
        + "\n"
        + _WINDOWS_DECLINED_LOG
        + _marker()
        + '\n[updater] force-checkout to "def5678"\n',
    )

    outcome = uo.last_updater_outcome()

    assert outcome is not None
    assert outcome.kind == "unknown"
    assert "def5678" in outcome.detail
    assert "DECLINED" not in outcome.detail


def test_this_runs_own_decline_still_travels_with_its_reason(windows_log: Path) -> None:
    """The narrowing must not cost Windows the one signal it has, nor the reason the
    operator acts on. A refusal after this run's marker is still a refusal — what is
    dropped is the earlier run's complaint, which named a problem this host may no
    longer have."""
    _write_log(
        windows_log,
        "  ✗ refusing self-update: a previous run said so\n"
        + _marker()
        + "\n[updater] restart DECLINED by its own preflight -- host still serving\n"
        + "  ✗ gateway unreachable at http://100.64.0.2:8000\n",
    )

    outcome = uo.last_updater_outcome()

    assert outcome is not None
    assert outcome.kind == "declined"
    assert "gateway unreachable" in outcome.detail
    assert "a previous run said so" not in outcome.detail


def test_no_marker_at_all_reads_exactly_as_it_did_before(windows_log: Path) -> None:
    """Two things produce a markerless tail, and neither is a failure to anchor: a run
    that has already written more than `_TAIL_BYTES` (so the tail cannot reach back
    into the previous run anyway), and an updater spawned by code that predates the
    marker — the rollout that ships it. Both read as they always did."""
    _write_log(windows_log, _WINDOWS_DECLINED_LOG)

    outcome = uo.last_updater_outcome()

    assert outcome is not None
    assert outcome.kind == "declined"


def test_a_marker_glued_onto_a_killed_runs_partial_line_still_anchors(
    windows_log: Path,
) -> None:
    """The case column zero alone cannot see.

    The previous run did not end at a line boundary — it was force-killed mid-write
    (the reaper does exactly this), so its last line reached the shared file without
    its newline. This run's marker is the very next thing appended, which lands it in
    the MIDDLE of that orphaned line rather than at the start of its own.

    Column zero is then structurally unreachable, so the primary rule finds nothing
    and the whole tail gets classified — handing back the killed run's refusal as this
    one's, which is issue #1117 again by another route. The epoch is what rescues it:
    the marker is still there, and it still carries an epoch no older than this
    update's pause.
    """
    _write_log(
        windows_log,
        _WINDOWS_DECLINED_LOG.rstrip("\n")
        + "\n[updater] fetching orig"  # killed mid-write: no newline after this
        + _marker()
        + '\n[updater] force-checkout to "def5678"\n',
    )

    outcome = uo.last_updater_outcome()

    assert outcome is not None
    assert outcome.kind == "unknown"
    assert "def5678" in outcome.detail
    assert "DECLINED" not in outcome.detail


class TestTheSlice:
    """`_anchor_to_this_run` on its own — the cases that decide whether a line is
    inside this run, tested where they can be stated exactly rather than through a
    log-shaped fixture."""

    def test_it_starts_after_the_last_marker_this_update_wrote(self) -> None:
        paused_at = time.time()
        tail = f"{_marker()}\nfirst try\n{_marker()}\nsecond try\n"

        assert uo._anchor_to_this_run(tail, paused_at) == "second try"

    def test_a_marker_older_than_the_pause_is_a_previous_runs(self) -> None:
        """A marker is only an anchor if it was written after this update paused the
        host. Anchoring on an older one would start the slice inside the previous run
        and hand back ITS whole output as this update's — the same misattribution,
        just harder to see."""
        tail = f"{_marker(age_s=3600.0)}\nthe previous run's output\n"

        assert uo._anchor_to_this_run(tail, time.time()) == tail

    def test_a_marker_shaped_line_inside_command_output_is_not_one(self) -> None:
        """The tail is mostly output this reader does not control — git's, uv's, the
        restart's — and a commit subject travels through `git checkout` verbatim. A
        marker must be at the start of its line, so a subject cannot decide which
        lines a host's update is judged by; the epoch it would have to forge (newer
        than a pause that had not happened when it was written) is the second lock."""
        forged = f"[updater-run] {int(time.time()) + 86400}"
        tail = f"{_marker()}\nmine\nHEAD is now at def5678 {forged}\n"

        assert uo._anchor_to_this_run(tail, time.time()) == f"mine\nHEAD is now at def5678 {forged}"

    def test_the_loose_pass_recovers_a_marker_a_kill_denied_column_zero(self) -> None:
        """The fallback, stated on its own: nothing is at column zero because the
        previous run's last line never got its newline, and the marker landed inside
        it. The slice still begins after that line."""
        tail = f"[updater] fetching orig{_marker()}\nmine\n"

        assert uo._anchor_to_this_run(tail, time.time()) == "mine"

    # The epoch is a placeholder filled in by the test body, and `ids` is spelled out,
    # because **a parametrize list is evaluated at collection time, once per xdist
    # worker**. A clock read here gives workers that straddle a second boundary
    # different values — and so different test IDs — which xdist reports as
    # "Different tests were collected between gw0 and gw4" and aborts the whole run.
    # Any clock this test needs belongs in its body, which runs once per worker on the
    # test it already agreed to run.
    @pytest.mark.parametrize(
        "embedded",
        [
            "[updater-run] {old_epoch}",  # a previous run's, echoed back
            "[updater-run]",  # no epoch to compare against a pause
            "[updater-run] later",  # nor a parseable one
        ],
        ids=["a-previous-runs-epoch", "no-epoch", "an-unparseable-epoch"],
    )
    def test_the_loose_pass_still_demands_a_current_epoch(self, embedded: str) -> None:
        """What keeps the fallback from becoming the hole the column-zero rule closed.

        Here the primary pass finds nothing, so the loose one is genuinely reached —
        and it must still refuse. Every shape a commit subject or a package line
        realistically carries is refused on its epoch alone: an old one (the subject of
        the very commit a previous run logged), or none at all. Anchoring on any of
        these would truncate the window to whatever followed some other run's text.
        """
        marker = embedded.format(old_epoch=int(time.time()) - 3600)
        tail = f"HEAD is now at def5678 {marker}\nthe run's real output\n"

        assert uo._anchor_to_this_run(tail, time.time()) == tail

    def test_what_the_loose_pass_costs_is_bounded_to_a_forged_future_epoch(self) -> None:
        """The boundary, named so it is a decision rather than a gap.

        Accepting a mid-line marker does give marker-shaped text *some* power — but
        only text carrying an epoch at least as new as a pause that had not happened
        when it was written. That is not a shape command output takes by accident, and
        the most it buys is a narrower diagnostic window on a host already mid-update.
        Column zero remains the primary rule precisely so this stays the exception.
        """
        forged = f"[updater-run] {int(time.time()) + 86400}"
        tail = f"HEAD is now at def5678 {forged}\nwhat follows\n"

        assert uo._anchor_to_this_run(tail, time.time()) == "what follows"

    @pytest.mark.parametrize(
        "line",
        [
            "HEAD is now at def5678 [updater-run] 4102444800",  # a commit subject
            "  [updater-run] 4102444800",  # uv indents its package lines
            "[updater-run]",  # no epoch to compare against a pause
            "[updater-run] later",  # nor a parseable one
        ],
    )
    def test_only_a_real_marker_is_read_as_one(self, line: str) -> None:
        assert uo._marker_epoch(line) is None

    def test_the_trailing_space_cmd_exe_echoes_is_forgiven(self) -> None:
        """`echo <marker> & <rest>` prints the space before the `&`, so the line as it
        lands is `<marker> \\r\\n`. The trailing end is the only end forgiven."""
        assert uo._marker_epoch("[updater-run] 4102444800 \r") == 4102444800

    def test_a_posix_tail_is_passed_through_untouched(self) -> None:
        """POSIX prints no marker — one log per spawn, nothing to separate — so the
        slice is the identity there, byte for byte."""
        assert uo._anchor_to_this_run(_DECLINED_LOG, time.time()) == _DECLINED_LOG


def test_the_line_the_updater_echoes_is_the_line_the_reader_anchors_on(
    windows_log: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Emitter and parser are one contract, so they are tested as one.

    `mark_native_run` builds a cmd.exe `echo <marker> & <rest>`, which prints the
    marker *and the space before the `&`* as its own CRLF-terminated line. Anything
    that drifts — the spelling, the trailing space, the line ending — silently stops
    anchoring, and a marker nothing anchors on is exactly the state before the fix.
    """
    # The emitter samples time.time() when it builds the command line, and the
    # assertion below re-spells the literal with its own time.time() sample; on a
    # busy shard the real work between the two (writing the log, reading it back)
    # can straddle a second boundary and the truncated epochs drift by one —
    # issue #123. Freeze the clock for the span so both literals sample the same
    # instant while each stays independently spelled.
    frozen = time.time()
    monkeypatch.setattr(time, "time", lambda: frozen)
    marked = uo.mark_native_run("git fetch origin")
    assert marked.endswith(" & git fetch origin")
    echoed = marked.removeprefix("echo ").removesuffix("& git fetch origin")
    _write_log(
        windows_log,
        _WINDOWS_DECLINED_LOG.replace("\n", "\r\n")
        + echoed
        + '\r\n[updater] force-checkout to "def5678"\r\n',
    )

    outcome = uo.last_updater_outcome()

    assert echoed.strip() == _marker(), "the emitted marker drifted from the spelling read here"
    assert outcome is not None
    assert outcome.kind == "unknown"
