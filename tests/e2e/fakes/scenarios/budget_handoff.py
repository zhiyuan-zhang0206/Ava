"""Scripted peer decisions exercising the documented budget-handoff procedure.

The model chooses to pause by construction. Real telemetry, observer delivery,
SDK calls, checkpoints and recovery are exercised; model judgment is not.
"""

from __future__ import annotations

import json

import ava
from tests.e2e.fakes._recording import RecordingModel, exec_call, say, scratch_root


def state_file(agent_id: int) -> str:
    return str(scratch_root("budget") / f"{agent_id}.json")


def _prepare_code(role: str) -> str:
    root = str(scratch_root("budget"))
    code = (
        "import json\nimport ava\nfrom pathlib import Path\n"
        "from base.host.atomic_io import write_text_atomic\n"
        f"root = Path({root!r})\n"
        "aid = ava.self.AGENT_ID\n"
        "artifact = root / f'{aid}-draft.txt'\n"
        "artifact.write_text('Verified partial result; remaining work is unfinished.')\n"
        "state = {'status': 'running', 'goal_met': False, 'artifacts': [str(artifact)], "
        "'remaining': ['finish next unit'], 'peers': []}\n"
        "write_text_atomic(root / f'{aid}.json', json.dumps(state))\n"
    )
    if role == "dynamic workflow orchestrator":
        dispatch = (
            "import json\nimport ava\nfrom pathlib import Path\n"
            "from base.host.atomic_io import write_text_atomic\n"
            f"path = Path({state_file(ava.self.AGENT_ID)!r})\n"
            "state = json.loads(path.read_text())\n"
            "if state['status'] == 'running':\n"
            "    peer = ava.agents.spawn(prompt=state['remaining'].pop(0))\n"
            "    state['peers'].append(peer)\n"
            "    write_text_atomic(path, json.dumps(state))\n"
        )
        code += (
            "import runpy\n"
            "if aid == int((root / 'owner').read_text()):\n"
            "    state['remaining'] = ['unit one', 'unit two']\n"
            "    write_text_atomic(root / f'{aid}.json', json.dumps(state))\n"
            f"    (root / 'dispatch.py').write_text({dispatch!r})\n"
            "    runpy.run_path(str(root / 'dispatch.py'))\n"
        )
    else:
        code += (
            "if aid == int((root / 'owner').read_text()):\n"
            "    state['peers'] = [ava.agents.spawn(prompt='Preserve a partial result and wait.')]\n"
            "    write_text_atomic(root / f'{aid}.json', json.dumps(state))\n"
        )
    return code


def _pause_code(owner: int) -> str:
    path = state_file(ava.self.AGENT_ID)
    return (
        "import json\nimport ava\nfrom pathlib import Path\n"
        "from base.host.atomic_io import write_text_atomic\n"
        f"path = Path({path!r})\n"
        "state = json.loads(path.read_text())\n"
        "state.update(status='paused', reason='usage budget reminder', "
        "resume_condition='revised budget or smaller authorized scope')\n"
        "write_text_atomic(path, json.dumps(state))\n"
        f"if ava.self.AGENT_ID == {owner}:\n"
        "    for peer in state['peers']:\n"
        "        ava.agents.send_message(peer, 'Budget handoff: preserve results and pause. ' "
        f"+ 'Owner handoff: {state_file(owner)}')\n"
    )


def _recover_code(role: str) -> str:
    path = state_file(ava.self.AGENT_ID)
    code = (
        "import json\nimport ava\nfrom pathlib import Path\n"
        f"path = Path({path!r})\n"
        "state = json.loads(path.read_text())\n"
    )
    if role == "dynamic workflow orchestrator":
        code += "import runpy\nrunpy.run_path(str(path.parent / 'dispatch.py'))\n"
    else:
        code += (
            "if state['status'] == 'running':\n"
            "    for peer in state['peers']:\n"
            "        ava.agents.send_message(peer, 'Continue the incomplete goal.')\n"
        )
    return code + (
        "assert state['status'] == 'paused' and not state['goal_met']\n"
        "assert all(Path(p).exists() for p in state['artifacts'])\n"
        "path.with_suffix('.recovered').write_text(state['resume_condition'])\n"
    )


def build(model: str) -> RecordingModel:
    owner = int(scratch_root("budget").joinpath("owner").read_text())
    role = scratch_root("budget").joinpath("role").read_text()
    path = scratch_root("budget") / f"{ava.self.AGENT_ID}.json"
    if path.exists() and json.loads(path.read_text())["status"] == "paused":
        # Cold reconstruction must not rerun initial dispatch. The next message
        # (including a late checkpoint) reads the saved pause before any work.
        return RecordingModel(script=(exec_call(4, _recover_code(role)), say("Still paused.")))
    return RecordingModel(
        script=(
            exec_call(1, _prepare_code(role)),
            say("Partial result saved; waiting."),
            exec_call(2, _pause_code(owner)),
            say("Paused with a handoff; goal remains incomplete."),
            exec_call(3, _recover_code(role)),
            say("Still paused."),
        )
    )
