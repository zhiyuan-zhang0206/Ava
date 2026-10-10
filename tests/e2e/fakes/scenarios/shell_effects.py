"""Real shell-session and watcher effects observed by a recording model."""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage
from pydantic import PrivateAttr

from tests.e2e.fakes.scenario_recording import RecordingModel, exec_call, say, scratch_root


def sandbox() -> Path:
    return scratch_root("shell")


def build_background(model: str, *, agent_id: int | None) -> RecordingModel:
    code = f"""
import ava
import time
from pathlib import Path
root = Path({str(sandbox())!r})
start = time.monotonic()
job = ava.shell.run_background("sleep 5; echo BG-TAIL-MARK; exit 7", name="e2e-background", ttl=120, notify="always")
(root / "background-handle").write_text(f"{{job.session_id}}\\n{{job.output_path}}\\n{{time.monotonic() - start}}")
print("background-started", job.session_id)
"""
    return RecordingModel(
        agent_id=agent_id, script=(exec_call(1, code), say("background started"), say("noticed"))
    )


def build_background_policy(model: str, *, agent_id: int | None) -> RecordingModel:
    code = f"""
import ava
from pathlib import Path
root = Path({str(sandbox())!r})
success = ava.shell.run_background("sleep 2; echo POLICY-SUCCESS", name="e2e-success", ttl=120)
failure = ava.shell.run_background("sleep 4; echo POLICY-FAILURE; exit 9", name="e2e-failure", ttl=120)
(root / "policy-handles").write_text(f"{{success.session_id}}\\n{{success.output_path}}\\n{{failure.session_id}}\\n{{failure.output_path}}")
print("policy-jobs", success.session_id, failure.session_id)
"""
    return RecordingModel(
        agent_id=agent_id, script=(exec_call(1, code), say("jobs started"), say("jobs buffered"))
    )


def build_session_verbs(model: str, *, agent_id: int | None) -> RecordingModel:
    code = f"""
import ava
import time
from pathlib import Path
root = Path({str(sandbox())!r})
sid = ava.shell.sessions.new("e2e-verbs", ttl=120)
(root / "session-id").write_text(str(sid))
ava.shell.sessions.send(sid, "echo SEND-MARK > " + str(root / "sent") + "; echo CAPTURE-MARK")
deadline = time.monotonic() + 10
while not (root / "sent").exists() and time.monotonic() < deadline:
    time.sleep(0.1)
print("capture-has-mark", "CAPTURE-MARK" in ava.shell.sessions.capture(sid))
ava.shell.sessions.send(sid, "echo KEY-MARK > " + str(root / "keys"), enter=False)
ava.shell.sessions.send_keys(sid, "Enter")
deadline = time.monotonic() + 10
while not (root / "keys").exists() and time.monotonic() < deadline:
    time.sleep(0.1)
print("listed-before", ava.shell.sessions.list())
print("renewed-until", ava.shell.sessions.renew(sid, ttl=240).isoformat())
ava.shell.sessions.kill(sid)
print("listed-after-kill", ava.shell.sessions.list())
ava.shell.sessions.new("e2e-extra", ttl=120)
print("kill-all-count", ava.shell.sessions.kill_all())
print("listed-after-kill-all", ava.shell.sessions.list())
"""
    return RecordingModel(
        agent_id=agent_id, script=(exec_call(1, code), say("session verbs finished"))
    )


def build_survival(model: str, *, agent_id: int | None) -> RecordingModel:
    root = sandbox()
    create = f"""
import ava
from pathlib import Path
root = Path({str(root)!r})
sid = ava.shell.sessions.new("e2e-survivor", ttl=300)
(root / "survivor-id").write_text(str(sid))
ava.shell.sessions.send(sid, "echo BEFORE-RESTART > " + str(root / "before"))
print("survivor-created", sid)
"""
    probe = f"""
import ava
from pathlib import Path
root = Path({str(root)!r})
sid = int((root / "survivor-id").read_text())
print("survivor-listed", ava.shell.sessions.list().get(sid))
ava.shell.sessions.send(sid, "echo AFTER-RESTART > " + str(root / "after"))
print("survivor-capture", ava.shell.sessions.capture(sid))
"""
    if (root / "survivor-id").exists():
        return RecordingModel(agent_id=agent_id, script=(exec_call(1, probe), say("survived")))
    return RecordingModel(agent_id=agent_id, script=(exec_call(1, create), say("ready")))


def build_watchers(model: str, *, agent_id: int | None) -> RecordingModel:
    code = f"""
import ava
import datetime
from pathlib import Path
root = Path({str(sandbox())!r})
launch = ava.watcher.launch(
    "print('LAUNCH-OUTPUT-MARK')",
    timeout="30s", name="e2e-launch", notify="always",
)
at = ava.watcher.at(datetime.timedelta(seconds=4), "AT-WAKE-MARK", name="e2e-at", notify="always")
cron = ava.watcher.cron("* * * * *", "CRON-WAKE-MARK", timezone="UTC",
    end_time=datetime.timedelta(seconds=90), name="e2e-cron", notify="always")
(root / "watcher-ids").write_text(f"{{launch}} {{at}} {{cron}}")
print("watchers-created", launch, at, cron)
"""
    return RecordingModel(
        agent_id=agent_id, script=(exec_call(1, code), *(say("watcher wake") for _ in range(8)))
    )


def build_watcher_timeout(model: str, *, agent_id: int | None) -> RecordingModel:
    code = f"""
import ava
from pathlib import Path
wid = ava.watcher.launch("import time; time.sleep(60)", timeout="2s",
    name="e2e-timeout", notify="always")
Path({str(sandbox() / "timeout-id")!r}).write_text(str(wid))
print("timeout-watcher", wid)
"""
    return RecordingModel(
        agent_id=agent_id,
        script=(exec_call(1, code), say("timeout pending"), say("timeout noticed")),
    )


class WatcherResurrectionModel(RecordingModel):
    """Consume the real wake and completion notices, separately or in one batch."""

    _wake_seen: bool = PrivateAttr(default=False)
    _completion_seen: bool = PrivateAttr(default=False)
    _response: AIMessage | None = PrivateAttr(default=None)

    def _record(self, messages: list[BaseMessage]) -> None:
        super()._record(messages)
        human = "\n".join(str(message.content) for message in messages if message.type == "human")
        wake = "RESURRECT-WAKE-MARK" in human and not self._wake_seen
        completion = "Watcher 'e2e-resurrect' finished." in human and not self._completion_seen
        if not wake and not completion:
            raise RuntimeError("watcher resurrection model received no new watcher notice")
        self._wake_seen |= wake
        self._completion_seen |= completion
        notices = [label for seen, label in ((wake, "wake"), (completion, "completion")) if seen]
        self._response = say("processed watcher " + " and ".join(notices))

    def _next_message(self) -> AIMessage:
        if self._response is None:
            raise RuntimeError("watcher resurrection model has no recorded notice to process")
        response, self._response = self._response, None
        return response


def build_watcher_resurrection(model: str, *, agent_id: int | None) -> RecordingModel:
    root = sandbox()
    if (root / "resurrection-id").exists():
        return WatcherResurrectionModel(agent_id=agent_id, script=())
    code = f"""
import ava
import datetime
from pathlib import Path
wid = ava.watcher.at(datetime.timedelta(seconds=15), "RESURRECT-WAKE-MARK",
    name="e2e-resurrect", notify="always")
Path({str(root / "resurrection-id")!r}).write_text(str(wid))
print("resurrection-watcher", wid)
"""
    return RecordingModel(agent_id=agent_id, script=(exec_call(1, code), say("watcher armed")))


def build_watcher_orphan(model: str, *, agent_id: int | None) -> RecordingModel:
    root = sandbox()
    watcher_code = (
        "import os, signal, time\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        f"with open({str(root / 'orphan-pid.pending')!r}, 'w') as f: f.write(str(os.getpid()))\n"
        f"os.replace({str(root / 'orphan-pid.pending')!r}, {str(root / 'orphan-pid')!r})\n"
        "while True: time.sleep(60)\n"
    )
    code = (
        "import ava\n"
        f"wid = ava.watcher.launch({watcher_code!r}, timeout='60s', name='e2e-orphan')\n"
        "print('orphan-watcher', wid)"
    )
    return RecordingModel(agent_id=agent_id, script=(exec_call(1, code), say("orphan guard armed")))
