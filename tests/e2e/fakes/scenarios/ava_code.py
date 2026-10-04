"""ava_code effect scenarios -- what the model is actually shown after coding-plugin calls.

A real agent runs a real turn: the scripted fake model asks for `execute_code`, the
real exec child runs the code against the real plugin, and the real hooks fire. The
only fake is the model, and it is a *recording* fake: every call writes the exact
messages it was handed to `model_inputs()`. Tests assert on those records (what the
model saw), on the on-disk result of the exec, and on the checkpoint -- never on a
function return value.

The world each scenario runs in (git repos with AGENTS.md / CLAUDE.md / project
skills) is planted by `seed_world()` under a temp root that is NOT below $HOME: the
context-file walk stops at the farthest of {git root, $HOME}, so a root below $HOME
would drag the developer's own AGENTS.md files into the assertions.

Paths are functions, not module constants: the pytest process imports this module at
collection time, before the e2e fixtures set AVA_HOME; the agent process imports it
after. Both resolve to the same root because both derive it from AVA_HOME's name.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import psycopg
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGenerationChunk, ChatResult

import ava
from base.config import settings
from tests.e2e.fakes._chat_model import ScriptedFakeChatModel

_USAGE = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

# Distinctive bodies the tests look for in the model's input.
ROOT_RULES = "ROOT-RULES-MARK: run the narrowest failing test first."
SUB_CLAUDE = "SUB-CLAUDE-MARK: keep helpers private."
PRIMARY_PATH_RULES = "PRIMARY-PATH-MARK: this file is read directly."
BIG_HEAD, BIG_MID, BIG_TAIL = "BIG-HEAD-MARK", "BIG-MID-MARK", "BIG-TAIL-MARK"
SKILL_NAME = "e2e-probe"
SKILL_DESCRIPTION = "Probe skill planted by the ava_code e2e."
FINAL = "scenario finished."


# -- world ---------------------------------------------------------------------


def root() -> Path:
    name = Path(os.environ["AVA_HOME"]).name
    return Path(tempfile.gettempdir()).resolve() / f"ava-e2e-code-{name}"


def project(name: str = "proj") -> Path:
    return root() / name


def record_path() -> Path:
    return root() / "model_inputs.jsonl"


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S603 -- fixed argv
    return path


def seed_world() -> None:
    """Plant the repos the scenarios walk. Wipes any earlier world first."""
    shutil.rmtree(root(), ignore_errors=True)
    main = _git_repo(project("proj"))
    (main / "AGENTS.md").write_text(ROOT_RULES + "\n")
    (main / "sub").mkdir()
    (main / "sub" / "CLAUDE.md").write_text(SUB_CLAUDE + "\n")
    (main / "sub" / "foo.py").write_text("# foo\n")
    (main / "sub" / "bar.py").write_text("# bar\n")
    skill = main / ".claude" / "skills" / SKILL_NAME
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {SKILL_NAME}\ndescription: {SKILL_DESCRIPTION}\n---\n\nProbe body.\n"
    )
    # Read AGENTS.md directly: the content reaches the model via the return value.
    direct = _git_repo(project("direct"))
    (direct / "AGENTS.md").write_text(PRIMARY_PATH_RULES + "\n")
    (direct / "z.py").write_text("# z\n")
    # Same AGENTS.md content as proj, different path: content-hash dedup.
    twin = _git_repo(project("twin"))
    (twin / "AGENTS.md").write_text(ROOT_RULES + "\n")
    (twin / "x.py").write_text("# x\n")
    # Oversized AGENTS.md: injected head+tail, full text archived.
    big = _git_repo(project("big"))
    filler = "." * settings.sandbox.exec_output_max_chars
    (big / "AGENTS.md").write_text(f"{BIG_HEAD}\n{filler}\n{BIG_MID}\n{filler}\n{BIG_TAIL}\n")
    (big / "y.py").write_text("# y\n")


# -- recording model -----------------------------------------------------------


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    blocks: list[Any] = list(content)
    return "\n".join(
        str(cast(dict[str, Any], b).get("text", "")) if isinstance(b, dict) else str(b)
        for b in blocks
    )


class RecordingModel(ScriptedFakeChatModel):
    """Scripted turns; before each one, append the messages it was handed to the record."""

    def _record(self, messages: list[BaseMessage]) -> None:
        entry = {
            "agent_id": ava.self.AGENT_ID,
            "pid": os.getpid(),
            "messages": [
                {
                    "type": m.type,
                    "text": _text(m.content),
                    "tag": m.additional_kwargs.get("ava_note_tag"),
                    "tool_calls": [tc["name"] for tc in getattr(m, "tool_calls", [])],
                }
                for m in messages
            ],
        }
        record_path().parent.mkdir(parents=True, exist_ok=True)
        with record_path().open("a") as f:
            f.write(json.dumps(entry) + "\n")

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        self._record(messages)
        yield self._make_chunk(self._next_message())

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        # The compaction summary is a non-streaming call; record it like the rest.
        self._record(messages)
        return await super()._agenerate(messages, stop, run_manager, **kwargs)


def model_inputs() -> list[list[dict[str, Any]]]:
    """Every model call so far, oldest first: that call's messages as the model saw them."""
    path = record_path()
    if not path.exists():
        return []
    return [json.loads(line)["messages"] for line in path.read_text().splitlines()]


# -- script helpers ------------------------------------------------------------


def _exec(n: int, code: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": f"call_{n}", "name": "execute_code", "args": {"code": code}}],
        usage_metadata=_USAGE,
    )


def _say(text: str) -> AIMessage:
    return AIMessage(content=text, usage_metadata=_USAGE)


def _read(path: Path) -> str:
    return f"import ava\nprint('read-ok', len(ava.files.read({str(path)!r})))"


# -- scenarios -----------------------------------------------------------------


def build_cwd_notes(model: str) -> RecordingModel:
    """Switch cwd inside the project, then run one more exec so a re-injection would show."""
    return RecordingModel(
        script=(
            _exec(1, f"import ava\nava.cwd.set({str(project())!r})\nprint('set-done')"),
            _exec(2, "import ava\nprint('probe', ava.cwd.get())"),
            _say(FINAL),
        )
    )


def build_context_files(model: str) -> RecordingModel:
    """Read files in repos with AGENTS.md / CLAUDE.md; every read is one script step."""
    return RecordingModel(
        script=(
            _exec(1, _read(project() / "sub" / "foo.py")),
            _exec(2, _read(project() / "sub" / "bar.py")),
            _exec(
                3, f"import ava\nprint(ava.files.read({str(project('direct') / 'AGENTS.md')!r}))"
            ),
            _exec(4, _read(project("direct") / "z.py")),
            _exec(5, _read(project("twin") / "x.py")),
            _exec(6, _read(project("big") / "y.py")),
            _say(FINAL),
        )
    )


def build_cwd_tools(model: str) -> RecordingModel:
    """Relative-path SDK calls follow the logical cwd, not the process cwd."""
    code = f"""
import os
import ava
proj = {str(project())!r}
ava.cwd.set(proj)
for bad in (proj + '/missing', proj + '/AGENTS.md'):
    try:
        ava.cwd.set(bad)
        print('set-unexpected-ok')
    except Exception as e:
        print('set-error', type(e).__name__)
ava.cwd.set('sub')
print('cwd', ava.cwd.get())
print('process-cwd-follows', os.getcwd() == str(ava.cwd.get()))
ava.files.write('out.txt', 'alpha\\n')
ava.files.append('out.txt', 'beta\\n')
ava.files.edit('out.txt', 'alpha', 'ALPHA')
print('glob', sorted(p.name for p in ava.files.glob('*.txt')))
ava.files.write('gone.txt', 'x')
ava.files.delete('gone.txt')
print('shell-pwd', ava.shell.run('pwd').strip())
"""
    return RecordingModel(script=(_exec(1, code), _say(FINAL)))


def _restart_applied() -> bool:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM inbound_messages WHERE agent_id = %s AND kind = 'restart' "
            "AND applied_at IS NOT NULL LIMIT 1",
            (ava.self.AGENT_ID,),
        )
        return cur.fetchone() is not None


def build_cwd_restart(model: str) -> RecordingModel:
    """Before a restart: move cwd into proj/sub. After: report the cwd the new process sees."""
    if _restart_applied():
        return RecordingModel(
            script=(
                _exec(1, "import ava\nprint('cwd-after-restart', ava.cwd.get())"),
                _say(FINAL),
            )
        )
    sub = project() / "sub"
    return RecordingModel(
        script=(_exec(1, f"import ava\nava.cwd.set({str(sub)!r})\nprint('set-done')"), _say(FINAL))
    )


def build_system_prompt(model: str) -> RecordingModel:
    return RecordingModel(script=(_say(FINAL),))


def build_after_compact(model: str) -> RecordingModel:
    """Surface notes, compact, then read again: the compact must make them resurface.

    Script positions: 1-2 first turn; 3 the compaction summary call; 4 the
    post-compact narration; 5-6 the second user turn.
    """
    first = (
        f"import ava\nava.cwd.set({str(project())!r})\n"
        f"print('read-ok', len(ava.files.read({str(project() / 'sub' / 'foo.py')!r})))"
    )
    return RecordingModel(
        script=(
            _exec(1, first),
            _say("ready."),
            _say("summary of the conversation so far."),
            _say("context compacted, continuing."),
            _exec(2, _read(project() / "sub" / "bar.py")),
            _say(FINAL),
        )
    )
