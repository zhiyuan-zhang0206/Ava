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

from ava.shell.coding_tools import _claude_checks, _common, claude
from base.sessions import coding_session_owner
from base.sessions.coding_session_owner_record import CodingSessionStatus

_SKILL_DIR = (
    Path(__file__).parents[4]
    / "ava_builtins"
    / "skills"
    / "platform"
    / "ava-guide"
    / "external-agents"
)


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


spawn_claude = _load("spawn_claude_under_test", _SKILL_DIR / "scripts" / "spawn_claude.py")


def _launch_takeover(workspace: Path, brief: str) -> int:
    """A one-hour "Fix login" takeover of ``workspace`` through the library entry."""
    return claude.launch(
        workspace, None, None, 3600, None, "Fix login", brief, skill_dir=_SKILL_DIR
    )


def _unwrap_paste(text: str) -> str:
    """The pasted bootstrap body; the operator's own words must follow the paste."""
    assert text.startswith(_claude_checks._PASTE_BEGIN)
    assert text.endswith(_claude_checks._PASTE_END + _claude_checks._OWN_WORDS)
    return text[len(_claude_checks._PASTE_BEGIN) : text.index(_claude_checks._PASTE_END)]


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
        status=CodingSessionStatus.ACTIVE,
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

    command = claude._claude_command(Path(record.key.workspace))

    assert command.startswith(f"cd {record.key.workspace} && ")
    assert "unset ANTHROPIC_API_KEY && " in command
    assert command.endswith('exec "$claude_bin" --dangerously-skip-permissions || exit $?')
    assert "AVA_CALLER_IDENTITY" not in command


def test_launch_command_can_explicitly_declare_external_caller(tmp_path: Path) -> None:
    record = _owner(tmp_path)

    command = claude._claude_command(Path(record.key.workspace), "run-42")

    assert "AVA_CALLER_IDENTITY=" in command
    assert '"kind":"external_agent"' in command
    assert '"subject":"claude_code"' in command
    assert '"instance":"run-42"' in command


def test_relay_command_wiring_is_default_on_and_opt_out_clean(tmp_path: Path) -> None:
    """Resident wiring: the plugin command carries the stub export; the opt-out stays silent."""
    record = _owner(tmp_path)
    workspace = Path(record.key.workspace)
    plugin = _SKILL_DIR / "scripts" / "ava-relay"
    stub = tmp_path / "generation" / "relay.env"

    resident = claude._claude_command(workspace, relay_stub=stub, relay_plugin_dir=plugin)
    manual = claude._claude_command(workspace)
    with pytest.raises(ValueError, match="stub path and its plugin dir"):
        claude._claude_command(workspace, relay_plugin_dir=plugin)

    assert f"export AVA_IMPERSONATION_RELAY_STUB={stub.as_posix()} " in resident
    assert "AVA_IMPERSONATION_RELAY_PY=" in resident
    assert resident.endswith(
        f'exec "$claude_bin" --dangerously-skip-permissions --plugin-dir {plugin.as_posix()} || exit $?'
    )
    assert "AVA_IMPERSONATION_RELAY_STUB" not in manual
    assert "--plugin-dir" not in manual

    fallback = claude._takeover_bootstrap_message(
        1, "Fix login", "brief", _common.impersonator_guide(), relay_resident=False
    )
    resident_message = claude._takeover_bootstrap_message(
        1, "Fix login", "brief", _common.impersonator_guide(), relay_resident=True
    )
    assert "Immediately start the Claude Monitor relay" in fallback
    assert "do not arm a Monitor watch" in resident_message


def test_takeover_never_delivers_bootstrap_to_a_non_claude_panel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = _owner(tmp_path)
    launching = replace(
        active, status=CodingSessionStatus.LAUNCHING, session_id=None, session_name=None
    )
    sent: list[str] = []
    killed: list[int] = []
    terminated: list[str] = []

    def _kill(sid: int) -> None:
        killed.append(sid)

    def _claim(*_args: object, **_kwargs: object) -> coding_session_owner.CodingSessionOwner:
        return launching

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

    original_command = claude._claude_command

    def _missing_command(
        workspace: Path,
        caller_instance: str | None = None,
        *,
        failure_marker: Path,
        relay_stub: Path | None = None,
        relay_plugin_dir: Path | None = None,
        claude_session: str | None = None,
        resume: bool = False,
    ) -> str:
        failure_marker.write_text("claude executable not found\n")
        return original_command(
            workspace,
            caller_instance,
            failure_marker=failure_marker,
            relay_stub=relay_stub,
            relay_plugin_dir=relay_plugin_dir,
            claude_session=claude_session,
            resume=resume,
        )

    def _publish(*_args: object, **_kwargs: object) -> coding_session_owner.CodingSessionOwner:
        return active

    def _terminate(_key: object, generation: str, *, reason: str) -> bool:
        assert reason == "launch-failed"
        terminated.append(generation)
        return True

    def _receipt(_sid: int, _rebuild: Callable[[], str], _submitted: object) -> None:
        pytest.fail("bootstrap sent")

    monkeypatch.setattr(claude, "new_generation", _claim)
    monkeypatch.setattr(claude, "_pretrust", _pretrust)
    monkeypatch.setattr(claude.ava.shell.sessions, "new", _new)
    monkeypatch.setattr(claude.ava.shell.sessions, "send", _send)
    monkeypatch.setattr(claude.ava.shell.sessions, "capture", _capture)
    monkeypatch.setattr(claude.ava.shell.sessions, "kill", _kill)
    monkeypatch.setattr(claude, "_claude_command", _missing_command)
    monkeypatch.setattr(claude.coding_session_owner, "publish_active", _publish)
    monkeypatch.setattr(claude.coding_session_owner, "terminate_generation", _terminate)
    monkeypatch.setattr(claude, "_verify_start_receipt", _receipt)

    with pytest.raises(RuntimeError, match="claude executable not found"):
        claude._run_takeover_launch(
            Path(active.key.workspace),
            "Fix login",
            "brief",
            3600,
            None,
            _SKILL_DIR,
            "11111111-2222-3333-4444-555555555555",
            relay_resident=False,
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
        status=CodingSessionStatus.LAUNCHING,
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
    ) -> coding_session_owner.CodingSessionOwner:
        assert tasks_file is None and work_file is None
        assert ttl_seconds == 3600
        events.append("claim")
        return launching

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

    def _receipt(
        _session_id: int, rebuild_bootstrap: Callable[[], str], _submitted: object
    ) -> None:
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
        return replace(
            launching, status=CodingSessionStatus.ACTIVE, session_id=7, session_name=session_name
        )

    monkeypatch.setattr(claude, "new_generation", _claim)
    monkeypatch.setattr(claude, "init_file", _unexpected)
    monkeypatch.setattr(claude, "_pretrust", _pretrust)
    monkeypatch.setattr(claude.ava.shell.sessions, "new", _new)
    monkeypatch.setattr(claude.ava.shell.sessions, "send", _send)
    monkeypatch.setattr(claude, "_wait_for_ready", _ready)
    monkeypatch.setattr(claude, "_verify_start_receipt", _receipt)
    monkeypatch.setattr(claude.coding_session_owner, "publish_active", _publish)

    workspace = Path(launching.key.workspace)
    brief = "Goal: replace the agent. The briefing is inline; read no files."
    rc = _launch_takeover(workspace, brief)

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


def test_resident_launch_scopes_the_credential_stub_to_its_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An earlier session's wrapper, orphaned in the same workspace, polls its own
    generation's dir; the new stub lives only in the new generation's private dir."""
    active = _owner(tmp_path)
    launching = replace(
        active, status=CodingSessionStatus.LAUNCHING, session_id=None, session_name=None
    )
    sent: list[str] = []

    def _claim(*_args: object, **_kwargs: object) -> coding_session_owner.CodingSessionOwner:
        return launching

    def _pretrust(_workspace: Path) -> None:
        return None

    def _new(**_kwargs: object) -> int:
        return 7

    def _send(_sid: int, content: str) -> None:
        sent.append(content)

    def _ready(_sid: int, *, failure_marker: Path, resumed: bool) -> None:
        assert not resumed
        assert failure_marker.parent.is_dir()

    def _receipt(_sid: int, _rebuild: Callable[[], str], _submitted: object) -> None:
        return None

    def _publish(
        _key: coding_session_owner.CodingSessionKey,
        _generation: str,
        *,
        session_id: int,
        session_name: str,
    ) -> coding_session_owner.CodingSessionOwner:
        return replace(
            launching,
            status=CodingSessionStatus.ACTIVE,
            session_id=session_id,
            session_name=session_name,
        )

    monkeypatch.setattr(claude, "new_generation", _claim)
    monkeypatch.setattr(claude, "_pretrust", _pretrust)
    monkeypatch.setattr(claude.ava.shell.sessions, "new", _new)
    monkeypatch.setattr(claude.ava.shell.sessions, "send", _send)
    monkeypatch.setattr(claude, "_wait_for_ready", _ready)
    monkeypatch.setattr(claude, "_verify_start_receipt", _receipt)
    monkeypatch.setattr(claude.coding_session_owner, "publish_active", _publish)

    workspace = Path(launching.key.workspace)
    assert launching.state_dir is not None
    stub = launching.state_dir / "relay.env"

    assert _launch_takeover(workspace, "brief") == 0

    assert launching.state_dir.is_dir()
    assert launching.state_dir.stat().st_mode & 0o777 == 0o700
    assert f"export AVA_IMPERSONATION_RELAY_STUB={stub.as_posix()} " in sent[0]
    assert workspace.as_posix() not in stub.as_posix()
    assert "--plugin-dir" in sent[0]


@pytest.mark.skipif(os.name == "nt", reason="resident wrapper requires POSIX")
def test_relay_wrapper_marks_the_standby_stub_consumed(tmp_path: Path) -> None:
    stub = tmp_path / ".ava-relay.env"
    stub.write_text("SID=9\nAGENT=41\nAVA_IMPERSONATION_RELAY_TOKEN=token\n", encoding="utf-8")
    wrapper = _SKILL_DIR / "scripts" / "ava-relay" / "scripts" / "relay-wrapper.sh"
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

    def no_receipt() -> bool:
        return False

    def no_sleep(_seconds: float) -> None:
        return None

    def dead_keys(_sid: int, *_keys: str) -> None:
        raise ValueError("session 7 is not this agent's (no match for 'shell-7')")

    def unexpected_capture(_sid: int, **_kwargs: object) -> str:
        raise AssertionError("capture must not run once the Enter retry refused")

    monkeypatch.setattr(_claude_checks.time, "sleep", no_sleep)
    monkeypatch.setattr(claude.ava.shell.sessions, "send_keys", dead_keys)
    monkeypatch.setattr(claude.ava.shell.sessions, "capture", unexpected_capture)

    _claude_checks._verify_start_receipt(7, lambda: "bootstrap", no_receipt, timeout=0.01)
    out = capsys.readouterr().out
    assert "start-receipt=not-submitted" in out
    assert "Enter retry failed" in out


def test_start_receipt_survives_a_dead_session_at_capture(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dead pane's capture refusal stays loud-not-fatal: warn, do not raise."""

    def no_receipt() -> bool:
        return False

    def no_sleep(_seconds: float) -> None:
        return None

    def no_keys(_sid: int, *_keys: str) -> None:
        return None

    def no_send(_sid: int, _message: str) -> None:
        return None

    def dead_capture(_sid: int, **_kwargs: object) -> str:
        raise ValueError("session 7 is not this agent's (no match for 'shell-7')")

    monkeypatch.setattr(_claude_checks.time, "sleep", no_sleep)
    monkeypatch.setattr(claude.ava.shell.sessions, "send_keys", no_keys)
    monkeypatch.setattr(claude.ava.shell.sessions, "send", no_send)
    monkeypatch.setattr(claude.ava.shell.sessions, "capture", dead_capture)

    _claude_checks._verify_start_receipt(7, lambda: "bootstrap", no_receipt, timeout=0.01)
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

    def transcript_evidence() -> bool:
        return next(evidence)

    def no_sleep(_seconds: float) -> None:
        return None

    def send_enter(_sid: int, key: str) -> None:
        keys.append(key)

    def resend_bootstrap(_sid: int, message: str) -> None:
        resend.append(message)

    def unexpected_capture(_sid: int) -> str:
        pytest.fail("capture is unnecessary once the rebuilt message is submitted")

    monkeypatch.setattr(_claude_checks.time, "sleep", no_sleep)
    monkeypatch.setattr(claude.ava.shell.sessions, "send_keys", send_enter)
    monkeypatch.setattr(claude.ava.shell.sessions, "send", resend_bootstrap)
    monkeypatch.setattr(claude.ava.shell.sessions, "capture", unexpected_capture)

    def rebuild_bootstrap() -> str:
        rebuilt.append("formal bootstrap")
        return rebuilt[-1]

    _claude_checks._verify_start_receipt(7, rebuild_bootstrap, transcript_evidence, timeout=0.0)

    assert keys == ["Enter"]
    assert rebuilt == ["formal bootstrap"]
    assert resend == ["formal bootstrap"]
    assert "start-receipt=submitted after rebuild resend" in capsys.readouterr().out


def test_start_receipt_does_not_rebuild_when_the_initial_bootstrap_is_submitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A submitted initial bootstrap never reaches either recovery send path."""

    def transcript_evidence() -> bool:
        return True

    def unexpected_enter(_sid: int, _key: str) -> None:
        pytest.fail("submitted bootstrap must not receive Enter")

    def unexpected_resend(_sid: int, _message: str) -> None:
        pytest.fail("submitted bootstrap must not be resent")

    monkeypatch.setattr(claude.ava.shell.sessions, "send_keys", unexpected_enter)
    monkeypatch.setattr(claude.ava.shell.sessions, "send", unexpected_resend)

    _claude_checks._verify_start_receipt(
        7,
        lambda: pytest.fail("submitted bootstrap must not be rebuilt"),
        transcript_evidence,
    )


def test_start_receipt_warns_after_one_unconfirmed_rebuild_resend(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unconfirmed rebuild stays loud and cannot enter another recovery cycle."""
    evidence = iter([False, False, False])
    rebuilt: list[str] = []
    resend: list[str] = []

    def transcript_evidence() -> bool:
        return next(evidence)

    def no_sleep(_seconds: float) -> None:
        return None

    def no_enter(_sid: int, _key: str) -> None:
        return None

    def resend_bootstrap(_sid: int, message: str) -> None:
        resend.append(message)

    def blank_capture(_sid: int) -> str:
        return ""

    monkeypatch.setattr(_claude_checks.time, "sleep", no_sleep)
    monkeypatch.setattr(claude.ava.shell.sessions, "send_keys", no_enter)
    monkeypatch.setattr(claude.ava.shell.sessions, "send", resend_bootstrap)
    monkeypatch.setattr(claude.ava.shell.sessions, "capture", blank_capture)

    def rebuild_bootstrap() -> str:
        rebuilt.append("formal bootstrap")
        return rebuilt[-1]

    _claude_checks._verify_start_receipt(7, rebuild_bootstrap, transcript_evidence, timeout=0.0)

    assert rebuilt == ["formal bootstrap"]
    assert resend == ["formal bootstrap"]
    out = capsys.readouterr().out
    assert "start-receipt=not-submitted" in out
    assert "after one rebuild resend" in out


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
    monkeypatch.setattr("ava.sdk_surface.agent_identity.require_agent_id", lambda: 41)
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
        claude.launch(tmp_path, None, None, 3600, skill_dir=_SKILL_DIR)


def test_a_new_generation_lets_the_sweep_ask_about_terminated_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _owner(tmp_path)
    seen: list[object] = []

    def _launch_generation(
        _key: coding_session_owner.CodingSessionKey, **kwargs: object
    ) -> coding_session_owner.CodingSessionOwner:
        seen.append(kwargs["owner_terminated"])
        return record

    monkeypatch.setattr(claude.coding_session_owner, "launch_generation", _launch_generation)

    assert (
        claude.new_generation(record.key, tasks_file=None, work_file=None, ttl_seconds=3600)
        is record
    )
    assert seen == [_common.owner_terminated]


def test_failed_early_publish_kills_claude_session_before_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = _owner(tmp_path)
    launching = replace(
        active,
        status=CodingSessionStatus.LAUNCHING,
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
    ) -> coding_session_owner.CodingSessionOwner:
        events.append("claim")
        return launching

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

    monkeypatch.setattr(claude, "new_generation", _claim)
    monkeypatch.setattr(claude, "_pretrust", _pretrust)
    monkeypatch.setattr(claude.ava.shell.sessions, "new", _new)
    monkeypatch.setattr(claude.ava.shell.sessions, "send", _send)
    monkeypatch.setattr(claude, "_wait_for_ready", _ready)
    monkeypatch.setattr(claude.coding_session_owner, "publish_active", _publish)
    monkeypatch.setattr(claude.ava.shell.sessions, "kill", _kill)
    monkeypatch.setattr(claude.coding_session_owner, "terminate_generation", _terminate)

    workspace = Path(launching.key.workspace)
    with pytest.raises(coding_session_owner.CodingSessionGenerationChangedError):
        _launch_takeover(workspace, "the briefing")

    assert events == ["claim", "pretrust", "new", "publish"]
    assert killed == [7]
    assert terminated == [(launching.generation, "launch-failed")]


def test_bracketed_paste_wraps_a_multi_chunk_payload() -> None:
    payload = "x" * (_claude_checks._PASTE_WRAP_THRESHOLD_CHARS + 1)
    assert _claude_checks._bracketed_paste(payload) == f"\x1b[200~{payload}\x1b[201~"


def test_bracketed_paste_leaves_a_single_chunk_payload_unchanged() -> None:
    payload = "x" * _claude_checks._PASTE_WRAP_THRESHOLD_CHARS
    assert _claude_checks._bracketed_paste(payload) == payload


def test_bracketed_paste_strips_inner_markers() -> None:
    payload = "\x1b[201~" + "y" * (_claude_checks._PASTE_WRAP_THRESHOLD_CHARS + 1) + "\x1b[200~"
    wrapped = _claude_checks._bracketed_paste(payload)
    assert wrapped.startswith("\x1b[200~") and wrapped.endswith("\x1b[201~")
    assert wrapped.count("\x1b[200~") == 1
    assert wrapped.count("\x1b[201~") == 1


def test_start_receipt_resend_wraps_a_multi_chunk_rebuilt_bootstrap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rebuild resend carries the bracketed wrap (#4364)."""
    evidence = iter([False, False, True])
    resend: list[str] = []

    def transcript_evidence() -> bool:
        return next(evidence)

    def no_sleep(_seconds: float) -> None:
        return None

    def send_enter(_sid: int, _key: str) -> None:
        return None

    def resend_bootstrap(_sid: int, message: str) -> None:
        resend.append(message)

    def unexpected_capture(_sid: int) -> str:
        pytest.fail("capture is unnecessary once the rebuilt message is submitted")

    monkeypatch.setattr(_claude_checks.time, "sleep", no_sleep)
    monkeypatch.setattr(claude.ava.shell.sessions, "send_keys", send_enter)
    monkeypatch.setattr(claude.ava.shell.sessions, "send", resend_bootstrap)
    monkeypatch.setattr(claude.ava.shell.sessions, "capture", unexpected_capture)

    rebuilt = "b" * (_claude_checks._PASTE_WRAP_THRESHOLD_CHARS + 1)
    _claude_checks._verify_start_receipt(7, lambda: rebuilt, transcript_evidence, timeout=0.0)

    assert resend == [f"\x1b[200~{rebuilt}\x1b[201~{_claude_checks._OWN_WORDS}"]
    assert "start-receipt=submitted after rebuild resend" in capsys.readouterr().out
