"""Skill-level contracts for the isolated Claude Code launch and its file-less takeover."""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

from shared import coding_session_owner

_REFERENCE = (
    Path(__file__).parents[2] / "ava_builtins" / "skills" / "ava-use-other-agents" / "reference"
)


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


spawn_claude = _load("spawn_claude_under_test", _REFERENCE / "spawn_claude.py")


def _unwrap_paste(text: str) -> str:
    assert text.startswith(spawn_claude._PASTE_BEGIN)
    assert text.endswith(spawn_claude._PASTE_END)
    return text[len(spawn_claude._PASTE_BEGIN) : -len(spawn_claude._PASTE_END)]


def _owner(tmp_path: Path) -> coding_session_owner.CodingSessionOwner:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    key = coding_session_owner.canonical_key(
        workspace,
        tool="claude",
        cluster=tmp_path / "cluster",
    )
    generation = "11111111-2222-3333-4444-555555555555"
    now = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
    return coding_session_owner.CodingSessionOwner(
        key=key,
        status="active",
        generation=generation,
        owner_agent_id=41,
        display_label="workspace",
        expected_suffix="claude-workspace-11111111",
        session_id=7,
        session_name="ava-agent-41-shell-7-claude-workspace-11111111",
        state_dir=coding_session_owner.generation_state_dir(key, generation),
        tasks_file=None,
        work_file=None,
        created_at=now,
        expires_at=now + dt.timedelta(hours=24),
    )


def test_launch_command_unsets_api_key_and_skips_permission_prompts(tmp_path: Path) -> None:
    record = _owner(tmp_path)

    command = spawn_claude._claude_command(Path(record.key.workspace))

    assert command.startswith(f"cd {record.key.workspace} && ")
    assert "unset ANTHROPIC_API_KEY && " in command
    assert command.endswith('exec "$claude_bin" --dangerously-skip-permissions || exit $?')
    assert "AVA_CALLER_IDENTITY" not in command


def test_launch_command_can_explicitly_declare_external_caller(tmp_path: Path) -> None:
    record = _owner(tmp_path)

    command = spawn_claude._claude_command(Path(record.key.workspace), "run-42")

    assert "AVA_CALLER_IDENTITY=" in command
    assert '"kind":"external_agent"' in command
    assert '"subject":"claude_code"' in command
    assert '"instance":"run-42"' in command


def test_relay_command_wiring_is_default_on_and_opt_out_clean(tmp_path: Path) -> None:
    """Resident wiring: the plugin command carries the stub export; the opt-out stays silent."""
    record = _owner(tmp_path)
    workspace = Path(record.key.workspace)
    plugin = spawn_claude._HERE / "ava-relay"
    stub = spawn_claude._relay_stub_path(workspace)

    resident = spawn_claude._claude_command(workspace, relay_plugin_dir=plugin)
    manual = spawn_claude._claude_command(workspace)

    assert f"export AVA_IMPERSONATION_RELAY_STUB={stub.as_posix()} " in resident
    assert "AVA_IMPERSONATION_RELAY_PY=" in resident
    assert resident.endswith(
        f'exec "$claude_bin" --dangerously-skip-permissions --plugin-dir {plugin.as_posix()} || exit $?'
    )
    assert "AVA_IMPERSONATION_RELAY_STUB" not in manual
    assert "--plugin-dir" not in manual

    fallback = spawn_claude._takeover_bootstrap_message(
        1, "Fix login", "brief", relay_resident=False
    )
    resident_message = spawn_claude._takeover_bootstrap_message(
        1, "Fix login", "brief", relay_resident=True
    )
    assert "Immediately start the Claude Monitor relay" in fallback
    assert "do not arm a Monitor watch" in resident_message


def test_takeover_never_delivers_bootstrap_to_a_non_claude_panel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = _owner(tmp_path)
    launching = replace(active, status="launching", session_id=None, session_name=None)
    sent: list[str] = []
    killed: list[int] = []
    terminated: list[str] = []

    def _kill(sid: int) -> None:
        killed.append(sid)

    def _claim(*_args: object, **_kwargs: object) -> coding_session_owner.CodingSessionClaim:
        return coding_session_owner.CodingSessionClaim(action="launch", owner=launching)

    def _pretrust(_workspace: Path) -> None:
        return None

    def _new(**_kwargs: object) -> int:
        return 7

    def _send(_sid: int, text: str) -> None:
        sent.append(text)

    def _capture(_sid: int, *, scrollback: bool) -> str:
        assert scrollback is False
        return (
            "error: claude executable not found in PATH or $HOME/.local/bin/claude\n"
            + "bash prompt $ " * 10
        )

    original_command = spawn_claude._claude_command

    def _missing_command(
        workspace: Path,
        caller_instance: str | None = None,
        *,
        failure_marker: Path,
        relay_plugin_dir: Path | None = None,
    ) -> str:
        failure_marker.write_text("claude executable not found\n")
        return original_command(
            workspace,
            caller_instance,
            failure_marker=failure_marker,
            relay_plugin_dir=relay_plugin_dir,
        )

    def _publish(*_args: object, **_kwargs: object) -> coding_session_owner.CodingSessionOwner:
        return active

    def _terminate(_key: object, generation: str, *, reason: str) -> bool:
        assert reason == "launch-failed"
        terminated.append(generation)
        return True

    def _receipt(_sid: int, _rebuild: Callable[[], str]) -> None:
        pytest.fail("bootstrap sent")

    monkeypatch.setattr(spawn_claude, "_claim_canonical", _claim)
    monkeypatch.setattr(spawn_claude, "_pretrust", _pretrust)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "new", _new)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "send", _send)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "capture", _capture)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "kill", _kill)
    monkeypatch.setattr(spawn_claude, "_claude_command", _missing_command)
    monkeypatch.setattr(spawn_claude.coding_session_owner, "publish_active", _publish)
    monkeypatch.setattr(spawn_claude.coding_session_owner, "terminate_generation", _terminate)
    monkeypatch.setattr(spawn_claude, "_verify_start_receipt", _receipt)

    with pytest.raises(RuntimeError, match="claude executable not found"):
        spawn_claude._run_takeover_launch(
            Path(active.key.workspace), "Fix login", "brief", 3600, None, relay_resident=False
        )

    assert len(sent) == 1
    assert killed == [7]
    assert terminated == [launching.generation]


def test_takeover_launch_inlines_brief_without_files_or_supervisor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _owner(tmp_path)
    launching = replace(
        active,
        status="launching",
        session_id=None,
        session_name=None,
        tasks_file=None,
        work_file=None,
    )
    events: list[str] = []
    sent: list[str] = []
    rebuilt: list[str] = []

    def _claim(
        _key: coding_session_owner.CodingSessionKey,
        *,
        tasks_file: Path | None,
        work_file: Path | None,
        ttl_seconds: float,
    ) -> coding_session_owner.CodingSessionClaim:
        assert tasks_file is None and work_file is None
        assert ttl_seconds == 3600
        events.append("claim")
        return coding_session_owner.CodingSessionClaim(action="launch", owner=launching)

    def _unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a takeover launch must not create files or start a supervisor")

    def _pretrust(_workspace: Path) -> None:
        events.append("pretrust")

    def _new(*, name: str, ttl: float) -> int:
        assert name == launching.expected_suffix
        assert ttl == 3600
        events.append("new")
        return 7

    def _send(session_id: int, content: str) -> None:
        assert session_id == 7
        sent.append(content)
        events.append("send")

    def _ready(_session_id: int, **_kwargs: object) -> None:
        events.append("ready")

    def _receipt(_session_id: int, rebuild_bootstrap: Callable[[], str]) -> None:
        rebuilt.append(rebuild_bootstrap())
        events.append("receipt")

    def _publish(
        _key: coding_session_owner.CodingSessionKey,
        _generation: str,
        *,
        session_id: int,
        session_name: str,
    ) -> coding_session_owner.CodingSessionOwner:
        assert session_id == 7
        assert session_name.endswith("-claude-workspace-11111111")
        events.append("publish")
        return replace(launching, status="active", session_id=7, session_name=session_name)

    monkeypatch.setattr(spawn_claude, "_claim_canonical", _claim)
    monkeypatch.setattr(spawn_claude, "_init_file", _unexpected)
    monkeypatch.setattr(spawn_claude, "_pretrust", _pretrust)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "new", _new)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "send", _send)
    monkeypatch.setattr(spawn_claude, "_wait_for_ready", _ready)
    monkeypatch.setattr(spawn_claude, "_verify_start_receipt", _receipt)
    monkeypatch.setattr(spawn_claude.coding_session_owner, "publish_active", _publish)

    workspace = Path(launching.key.workspace)
    brief = "Goal: replace the agent. The briefing is inline; read no files."
    rc = spawn_claude._launch(workspace, None, None, 3600, None, "Fix login", brief)

    assert rc == 0
    assert events == ["claim", "pretrust", "new", "publish", "send", "ready", "send", "receipt"]
    assert sent[0].startswith(f"cd {workspace.as_posix()} && ")
    # The bootstrap goes out as one bracketed paste so the composer cannot
    # fold it into content-dropping fragments (#4364).
    message = _unwrap_paste(sent[1])
    assert "take over Ava agent 41" in message
    assert brief in message
    assert "tasks.md" not in message and "work.md" not in message
    assert rebuilt == [message]


def test_resident_launch_clears_a_stale_credential_stub(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new takeover never consumes an earlier session's stub: cleared before the session starts."""
    active = _owner(tmp_path)
    launching = replace(active, status="launching", session_id=None, session_name=None)
    sent: list[str] = []

    def _claim(*_args: object, **_kwargs: object) -> coding_session_owner.CodingSessionClaim:
        return coding_session_owner.CodingSessionClaim(action="launch", owner=launching)

    def _pretrust(_workspace: Path) -> None:
        return None

    def _new(**_kwargs: object) -> int:
        return 7

    def _send(_sid: int, content: str) -> None:
        sent.append(content)

    def _ready(_sid: int, *, failure_marker: Path) -> None:
        assert failure_marker.parent.is_dir()

    def _receipt(_sid: int, _rebuild: Callable[[], str]) -> None:
        return None

    def _publish(
        _key: coding_session_owner.CodingSessionKey,
        _generation: str,
        *,
        session_id: int,
        session_name: str,
    ) -> coding_session_owner.CodingSessionOwner:
        return replace(launching, status="active", session_id=session_id, session_name=session_name)

    monkeypatch.setattr(spawn_claude, "_claim_canonical", _claim)
    monkeypatch.setattr(spawn_claude, "_pretrust", _pretrust)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "new", _new)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "send", _send)
    monkeypatch.setattr(spawn_claude, "_wait_for_ready", _ready)
    monkeypatch.setattr(spawn_claude, "_verify_start_receipt", _receipt)
    monkeypatch.setattr(spawn_claude.coding_session_owner, "publish_active", _publish)

    workspace = Path(launching.key.workspace)
    stub = spawn_claude._relay_stub_path(workspace)
    stub.write_text("SID=9\nAGENT=41\nAVA_IMPERSONATION_RELAY_TOKEN=stale\n", encoding="utf-8")
    marker = workspace / ".ava-relay.pid"
    marker.write_text("1234\n", encoding="utf-8")

    assert spawn_claude._launch(workspace, None, None, 3600, None, "Fix login", "brief") == 0

    assert not stub.exists()
    assert not marker.exists()
    assert f"export AVA_IMPERSONATION_RELAY_STUB={stub.as_posix()} " in sent[0]
    assert "--plugin-dir" in sent[0]


@pytest.mark.skipif(os.name == "nt", reason="resident wrapper requires POSIX")
def test_relay_wrapper_marks_the_standby_stub_consumed(tmp_path: Path) -> None:
    stub = tmp_path / ".ava-relay.env"
    stub.write_text("SID=9\nAGENT=41\nAVA_IMPERSONATION_RELAY_TOKEN=token\n", encoding="utf-8")
    wrapper = _REFERENCE / "ava-relay" / "scripts" / "relay-wrapper.sh"
    result = subprocess.run(  # noqa: S603 - wrapper path is fixed in this checkout
        ["bash", str(wrapper)],
        env=os.environ
        | {
            "AVA_IMPERSONATION_RELAY_STUB": str(stub),
            "AVA_IMPERSONATION_RELAY_PY": "/usr/bin/true",
            "AVA_IMPERSONATION_RELAY_STUB_WAIT_SECONDS": "1",
        },
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not stub.exists()
    assert int((tmp_path / ".ava-relay.pid").read_text(encoding="utf-8")) > 0


def test_start_receipt_survives_a_dead_session_at_the_enter_retry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dead pane's send_keys refusal stays loud-not-fatal: warn, do not raise.

    send_keys is the first call a mid-window death reaches (it precedes the
    capture read), so this is the throw point the contract must cover first.
    """

    def no_receipt(_started: float) -> bool:
        return False

    def no_sleep(_seconds: float) -> None:
        return None

    def dead_keys(_sid: int, *_keys: str) -> None:
        raise ValueError("session 7 is not this agent's (no match for 'shell-7')")

    def unexpected_capture(_sid: int, **_kwargs: object) -> str:
        raise AssertionError("capture must not run once the Enter retry refused")

    monkeypatch.setattr(spawn_claude, "_bootstrap_submitted", no_receipt)
    monkeypatch.setattr(spawn_claude.time, "sleep", no_sleep)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "send_keys", dead_keys)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "capture", unexpected_capture)

    spawn_claude._verify_start_receipt(7, lambda: "bootstrap", timeout=0.01)
    out = capsys.readouterr().out
    assert "start-receipt=not-submitted" in out
    assert "Enter retry failed" in out


def test_start_receipt_survives_a_dead_session_at_capture(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dead pane's capture refusal stays loud-not-fatal: warn, do not raise."""

    def no_receipt(_started: float) -> bool:
        return False

    def no_sleep(_seconds: float) -> None:
        return None

    def no_keys(_sid: int, *_keys: str) -> None:
        return None

    def no_send(_sid: int, _message: str) -> None:
        return None

    def dead_capture(_sid: int, **_kwargs: object) -> str:
        raise ValueError("session 7 is not this agent's (no match for 'shell-7')")

    monkeypatch.setattr(spawn_claude, "_bootstrap_submitted", no_receipt)
    monkeypatch.setattr(spawn_claude.time, "sleep", no_sleep)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "send_keys", no_keys)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "send", no_send)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "capture", dead_capture)

    spawn_claude._verify_start_receipt(7, lambda: "bootstrap", timeout=0.01)
    out = capsys.readouterr().out
    assert "start-receipt=not-submitted" in out
    assert "capture failed" in out


def test_start_receipt_rebuilds_and_resends_once_after_a_lost_bootstrap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A lost first send is rebuilt once; transcript evidence then ends recovery."""
    evidence = iter([False, False, True])
    rebuilt: list[str] = []
    resend: list[str] = []
    keys: list[str] = []

    def transcript_evidence(_started: float) -> bool:
        return next(evidence)

    def no_sleep(_seconds: float) -> None:
        return None

    def send_enter(_sid: int, key: str) -> None:
        keys.append(key)

    def resend_bootstrap(_sid: int, message: str) -> None:
        resend.append(message)

    def unexpected_capture(_sid: int) -> str:
        pytest.fail("capture is unnecessary once the rebuilt message is submitted")

    monkeypatch.setattr(spawn_claude, "_bootstrap_submitted", transcript_evidence)
    monkeypatch.setattr(spawn_claude.time, "sleep", no_sleep)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "send_keys", send_enter)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "send", resend_bootstrap)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "capture", unexpected_capture)

    def rebuild_bootstrap() -> str:
        rebuilt.append("formal bootstrap")
        return rebuilt[-1]

    spawn_claude._verify_start_receipt(7, rebuild_bootstrap, timeout=0.0)

    assert keys == ["Enter"]
    assert rebuilt == ["formal bootstrap"]
    assert resend == ["formal bootstrap"]
    assert "start-receipt=submitted after rebuild resend" in capsys.readouterr().out


def test_start_receipt_does_not_rebuild_when_the_initial_bootstrap_is_submitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A submitted initial bootstrap never reaches either recovery send path."""

    def transcript_evidence(_started: float) -> bool:
        return True

    def unexpected_enter(_sid: int, _key: str) -> None:
        pytest.fail("submitted bootstrap must not receive Enter")

    def unexpected_resend(_sid: int, _message: str) -> None:
        pytest.fail("submitted bootstrap must not be resent")

    monkeypatch.setattr(spawn_claude, "_bootstrap_submitted", transcript_evidence)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "send_keys", unexpected_enter)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "send", unexpected_resend)

    spawn_claude._verify_start_receipt(
        7,
        lambda: pytest.fail("submitted bootstrap must not be rebuilt"),
    )


def test_start_receipt_warns_after_one_unconfirmed_rebuild_resend(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unconfirmed rebuild stays loud and cannot enter another recovery cycle."""
    evidence = iter([False, False, False])
    rebuilt: list[str] = []
    resend: list[str] = []

    def transcript_evidence(_started: float) -> bool:
        return next(evidence)

    def no_sleep(_seconds: float) -> None:
        return None

    def no_enter(_sid: int, _key: str) -> None:
        return None

    def resend_bootstrap(_sid: int, message: str) -> None:
        resend.append(message)

    def blank_capture(_sid: int) -> str:
        return ""

    monkeypatch.setattr(spawn_claude, "_bootstrap_submitted", transcript_evidence)
    monkeypatch.setattr(spawn_claude.time, "sleep", no_sleep)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "send_keys", no_enter)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "send", resend_bootstrap)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "capture", blank_capture)

    def rebuild_bootstrap() -> str:
        rebuilt.append("formal bootstrap")
        return rebuilt[-1]

    spawn_claude._verify_start_receipt(7, rebuild_bootstrap, timeout=0.0)

    assert rebuilt == ["formal bootstrap"]
    assert resend == ["formal bootstrap"]
    out = capsys.readouterr().out
    assert "start-receipt=not-submitted" in out
    assert "after one rebuild resend" in out


def test_takeover_launch_refuses_a_workspace_with_a_live_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _owner(tmp_path)

    def _claim(
        _key: coding_session_owner.CodingSessionKey,
        *,
        tasks_file: Path | None,
        work_file: Path | None,
        ttl_seconds: float,
    ) -> coding_session_owner.CodingSessionClaim:
        assert tasks_file is None and work_file is None
        return coding_session_owner.CodingSessionClaim(action="adopt", owner=record)

    monkeypatch.setattr(spawn_claude, "_claim_canonical", _claim)
    stub = Path(record.key.workspace) / ".ava-relay.env"
    marker = Path(record.key.workspace) / ".ava-relay.pid"
    stub.write_text("current credential", encoding="utf-8")
    marker.write_text("1234\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="fresh coding workspace"):
        spawn_claude._launch(
            Path(record.key.workspace), None, None, 3600, None, "Fix login", "the briefing"
        )
    assert stub.read_text(encoding="utf-8") == "current credential"
    assert marker.read_text(encoding="utf-8") == "1234\n"


@pytest.mark.parametrize(
    "extra",
    [
        ["--brief", "briefing", "--tasks-file", "tasks.md"],
        ["--brief", "briefing", "--work-file", "work.md"],
        [],
        ["--brief", "   "],
    ],
)
def test_claude_takeover_cli_rejects_files_and_requires_a_brief(
    extra: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ava._boot.require_agent_id", lambda: 41)
    monkeypatch.setattr(
        sys,
        "argv",
        ["spawn_claude.py", str(tmp_path), "--impersonate-self", *extra],
    )

    with pytest.raises(SystemExit):
        spawn_claude.main()


def test_claude_brief_requires_takeover_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["spawn_claude.py", str(tmp_path), "--brief", "briefing"])

    with pytest.raises(SystemExit):
        spawn_claude.main()


def test_supervised_launch_needs_its_files(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="supervised launch needs its task and work files"):
        spawn_claude._launch(tmp_path, None, None, 3600)


def test_claim_reclaims_a_terminated_owners_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _owner(tmp_path)
    seen: list[str | None] = []

    def _claim(
        _key: coding_session_owner.CodingSessionKey,
        *,
        owner_agent_id: int,
        tasks_file: Path | None,
        work_file: Path | None,
        ttl_seconds: float,
        terminated_generation: str | None,
    ) -> coding_session_owner.CodingSessionClaim:
        seen.append(terminated_generation)
        return coding_session_owner.CodingSessionClaim(action="launch", owner=record)

    def _read(
        _key: coding_session_owner.CodingSessionKey,
    ) -> coding_session_owner.CodingSessionOwner:
        return record

    def _terminated(_agent_id: int) -> bool:
        return True

    monkeypatch.setattr(spawn_claude.coding_session_owner, "read", _read)
    monkeypatch.setattr(spawn_claude, "_owner_terminated", _terminated)
    monkeypatch.setattr(spawn_claude.coding_session_owner, "claim", _claim)

    spawn_claude._claim_canonical(record.key, tasks_file=None, work_file=None, ttl_seconds=3600)

    assert seen == [record.generation]


def test_failed_early_publish_kills_claude_session_before_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _owner(tmp_path)
    launching = replace(
        active,
        status="launching",
        session_id=None,
        session_name=None,
        tasks_file=None,
        work_file=None,
    )
    events: list[str] = []
    killed: list[int] = []
    terminated: list[tuple[str, str]] = []

    def _claim(
        _key: coding_session_owner.CodingSessionKey,
        *,
        tasks_file: Path | None,
        work_file: Path | None,
        ttl_seconds: float,
    ) -> coding_session_owner.CodingSessionClaim:
        events.append("claim")
        return coding_session_owner.CodingSessionClaim(action="launch", owner=launching)

    def _pretrust(_workspace: Path) -> None:
        events.append("pretrust")

    def _new(*, name: str, ttl: float) -> int:
        events.append("new")
        return 7

    def _send(_session_id: int, _content: str) -> None:
        events.append("send")

    def _ready(_session_id: int) -> None:
        events.append("ready")

    def _publish(
        _key: coding_session_owner.CodingSessionKey,
        _generation: str,
        *,
        session_id: int,
        session_name: str,
    ) -> coding_session_owner.CodingSessionOwner:
        events.append("publish")
        raise coding_session_owner.CodingSessionGenerationChangedError("replacement won")

    def _kill(session_id: int) -> None:
        killed.append(session_id)

    def _terminate(
        _key: coding_session_owner.CodingSessionKey,
        _generation: str,
        *,
        reason: str,
    ) -> bool:
        terminated.append((_generation, reason))
        return False

    monkeypatch.setattr(spawn_claude, "_claim_canonical", _claim)
    monkeypatch.setattr(spawn_claude, "_pretrust", _pretrust)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "new", _new)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "send", _send)
    monkeypatch.setattr(spawn_claude, "_wait_for_ready", _ready)
    monkeypatch.setattr(spawn_claude.coding_session_owner, "publish_active", _publish)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "kill", _kill)
    monkeypatch.setattr(spawn_claude.coding_session_owner, "terminate_generation", _terminate)

    workspace = Path(launching.key.workspace)
    with pytest.raises(coding_session_owner.CodingSessionGenerationChangedError):
        spawn_claude._launch(workspace, None, None, 3600, None, "Fix login", "the briefing")

    assert events == ["claim", "pretrust", "new", "publish"]
    assert killed == [7]
    assert terminated == [(launching.generation, "launch-failed")]


def test_bracketed_paste_wraps_a_multi_chunk_payload() -> None:
    payload = "x" * (spawn_claude._PASTE_WRAP_THRESHOLD_CHARS + 1)
    assert spawn_claude._bracketed_paste(payload) == f"\x1b[200~{payload}\x1b[201~"


def test_bracketed_paste_leaves_a_single_chunk_payload_unchanged() -> None:
    payload = "x" * spawn_claude._PASTE_WRAP_THRESHOLD_CHARS
    assert spawn_claude._bracketed_paste(payload) == payload


def test_bracketed_paste_strips_inner_markers() -> None:
    payload = "\x1b[201~" + "y" * (spawn_claude._PASTE_WRAP_THRESHOLD_CHARS + 1) + "\x1b[200~"
    wrapped = spawn_claude._bracketed_paste(payload)
    assert wrapped.startswith("\x1b[200~") and wrapped.endswith("\x1b[201~")
    assert wrapped.count("\x1b[200~") == 1
    assert wrapped.count("\x1b[201~") == 1


def test_start_receipt_resend_wraps_a_multi_chunk_rebuilt_bootstrap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rebuild resend carries the bracketed wrap (#4364)."""
    evidence = iter([False, False, True])
    resend: list[str] = []

    def transcript_evidence(_started: float) -> bool:
        return next(evidence)

    def no_sleep(_seconds: float) -> None:
        return None

    def send_enter(_sid: int, _key: str) -> None:
        return None

    def resend_bootstrap(_sid: int, message: str) -> None:
        resend.append(message)

    def unexpected_capture(_sid: int) -> str:
        pytest.fail("capture is unnecessary once the rebuilt message is submitted")

    monkeypatch.setattr(spawn_claude, "_bootstrap_submitted", transcript_evidence)
    monkeypatch.setattr(spawn_claude.time, "sleep", no_sleep)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "send_keys", send_enter)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "send", resend_bootstrap)
    monkeypatch.setattr(spawn_claude.ava.shell.sessions, "capture", unexpected_capture)

    rebuilt = "b" * (spawn_claude._PASTE_WRAP_THRESHOLD_CHARS + 1)
    spawn_claude._verify_start_receipt(7, lambda: rebuilt, timeout=0.0)

    assert resend == [f"\x1b[200~{rebuilt}\x1b[201~"]
    assert "start-receipt=submitted after rebuild resend" in capsys.readouterr().out
