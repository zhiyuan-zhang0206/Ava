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

import shutil
import subprocess
from pathlib import Path

import psycopg

from base.config import settings
from tests.e2e.fakes._recording import (
    RecordingModel,
    exec_call,
    model_inputs,
    reset_record,
    say,
    scratch_root,
)

__all__ = ["RecordingModel", "model_inputs"]

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
    return scratch_root("code")


def project(name: str = "proj") -> Path:
    return root() / name


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)  # noqa: S603 -- fixed argv
    return path


def seed_world() -> None:
    """Plant the repos the scenarios walk. Wipes any earlier world first."""
    shutil.rmtree(root(), ignore_errors=True)
    reset_record()
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


# -- script helpers ------------------------------------------------------------


def _read(path: Path) -> str:
    return f"import ava\nprint('read-ok', len(ava.files.read({str(path)!r})))"


# -- scenarios -----------------------------------------------------------------


def build_cwd_notes(model: str, *, agent_id: int | None) -> RecordingModel:
    """Switch cwd inside the project, then run one more exec so a re-injection would show."""
    return RecordingModel(
        agent_id=agent_id,
        script=(
            exec_call(1, f"import ava\nava.cwd.set({str(project())!r})\nprint('set-done')"),
            exec_call(2, "import ava\nprint('probe', ava.cwd.get())"),
            say(FINAL),
        ),
    )


def build_context_files(model: str, *, agent_id: int | None) -> RecordingModel:
    """Read files in repos with AGENTS.md / CLAUDE.md; every read is one script step."""
    return RecordingModel(
        agent_id=agent_id,
        script=(
            exec_call(1, _read(project() / "sub" / "foo.py")),
            exec_call(2, _read(project() / "sub" / "bar.py")),
            exec_call(
                3, f"import ava\nprint(ava.files.read({str(project('direct') / 'AGENTS.md')!r}))"
            ),
            exec_call(4, _read(project("direct") / "z.py")),
            exec_call(5, _read(project("twin") / "x.py")),
            exec_call(6, _read(project("big") / "y.py")),
            say(FINAL),
        ),
    )


def build_cwd_tools(model: str, *, agent_id: int | None) -> RecordingModel:
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
    return RecordingModel(agent_id=agent_id, script=(exec_call(1, code), say(FINAL)))


def _restart_applied(agent_id: int | None) -> bool:
    with psycopg.connect(settings.data_plane.db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM inbound_messages WHERE agent_id = %s AND kind = 'restart' "
            "AND applied_at IS NOT NULL LIMIT 1",
            (agent_id,),
        )
        return cur.fetchone() is not None


def build_cwd_restart(model: str, *, agent_id: int | None) -> RecordingModel:
    """Before a restart: move cwd into proj/sub. After: report the cwd the new process sees."""
    if _restart_applied(agent_id):
        return RecordingModel(
            agent_id=agent_id,
            script=(
                exec_call(1, "import ava\nprint('cwd-after-restart', ava.cwd.get())"),
                say(FINAL),
            ),
        )
    sub = project() / "sub"
    return RecordingModel(
        agent_id=agent_id,
        script=(
            exec_call(1, f"import ava\nava.cwd.set({str(sub)!r})\nprint('set-done')"),
            say(FINAL),
        ),
    )


def build_system_prompt(model: str, *, agent_id: int | None) -> RecordingModel:
    return RecordingModel(agent_id=agent_id, script=(say(FINAL),))


def build_after_compact(model: str, *, agent_id: int | None) -> RecordingModel:
    """Surface notes, compact, then read again: the compact must make them resurface.

    Script positions: 1-2 first turn; 3 the compaction summary call; 4 the
    post-compact narration; 5-6 the second user turn.
    """
    first = (
        f"import ava\nava.cwd.set({str(project())!r})\n"
        f"print('read-ok', len(ava.files.read({str(project() / 'sub' / 'foo.py')!r})))"
    )
    return RecordingModel(
        agent_id=agent_id,
        script=(
            exec_call(1, first),
            say("ready."),
            say("summary of the conversation so far."),
            say("context compacted, continuing."),
            exec_call(2, _read(project() / "sub" / "bar.py")),
            say(FINAL),
        ),
    )
