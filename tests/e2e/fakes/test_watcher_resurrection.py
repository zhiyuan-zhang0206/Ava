"""The resurrection fake accepts both legitimate watcher notification schedules."""

from pathlib import Path

import pytest
from langchain_core.messages import BaseMessage, HumanMessage

from base.native_process.turn_identity import bind_turn_identity
from tests.e2e.fakes.scenarios import shell_effects


@pytest.mark.parametrize("batched", [False, True])
async def test_resurrection_model_processes_wake_and_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batched: bool
) -> None:
    monkeypatch.setenv("AVA_HOME", str(tmp_path))
    shell_effects.sandbox().mkdir(parents=True, exist_ok=True)
    (shell_effects.sandbox() / "resurrection-id").write_text("0")
    model = shell_effects.build_watcher_resurrection("unused")
    messages: list[BaseMessage] = [HumanMessage("RESURRECT-WAKE-MARK")]
    completion = HumanMessage("Watcher 'e2e-resurrect' finished. Full output at watcher.log.")
    with bind_turn_identity(17):
        if batched:
            messages.append(completion)
            reply = await model.ainvoke(messages)
            assert reply.content == "processed watcher wake and completion"
        else:
            reply = await model.ainvoke(messages)
            assert reply.content == "processed watcher wake"
            messages.extend((reply, completion))
            reply = await model.ainvoke(messages)
            assert reply.content == "processed watcher completion"
        with pytest.raises(RuntimeError, match="no new watcher notice"):
            await model.ainvoke(messages)
