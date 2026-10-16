import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "rebuild_memory_index.py"


def _run(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AVA_")}
    env["AVA_HOME"] = str(tmp_path)
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )


def _note(pool: Path, rel: str, text: str) -> None:
    target = pool / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_the_rebuild_writes_one_pointer_line_per_note_and_directory(tmp_path: Path) -> None:
    pool = tmp_path / "memory"
    pool.mkdir()
    (pool / "MEMORY.md").write_text(
        "Preamble text.\n\n`## Pointers` is only quoted in prose.\n\n## Pointers\n\n- [stale](stale.md) \u2014 old\n",
        encoding="utf-8",
    )
    _note(pool, "AGENTS.md", "rules\n")
    _note(pool, "index.md", "index\n")
    _note(pool, "log.md", "log\n")
    _note(pool, "alpha.md", "---\ntitle: Alpha\ndescription: first note\n---\nbody\n")
    _note(pool, "beta.md", "not a frontmatter block\n")
    _note(pool, "people/bob.md", "---\ntitle: Bob\ndescription: person\n---\n")
    _note(pool, "people/carol.md", "---\ntitle: Carol\n---\n")
    _note(pool, "people/roles/lead.md", "---\ntitle: Lead\n---\n")

    res = _run(tmp_path)

    assert res.returncode == 0, res.stderr
    assert (pool / "MEMORY.md").read_text(encoding="utf-8") == (
        "Preamble text.\n\n`## Pointers` is only quoted in prose.\n\n"
        "## Pointers\n\n\n"
        "- [Alpha](alpha.md) \u2014 first note\n"
        "- [beta](beta.md) \u2014 \n"
        "- [people/](people/index.md) \u2014 3 notes: roles(1)\n"
    )


def test_a_pool_without_an_index_file_gets_one(tmp_path: Path) -> None:
    pool = tmp_path / "memory"
    pool.mkdir()
    _note(pool, "solo.md", "---\ntitle: Solo\ndescription: only note\n---\n")

    res = _run(tmp_path)

    assert res.returncode == 0, res.stderr
    assert (pool / "MEMORY.md").read_text(encoding="utf-8") == (
        "## Pointers\n\n\n- [Solo](solo.md) \u2014 only note\n"
    )


def test_a_missing_heading_appends_after_the_existing_preamble(tmp_path: Path) -> None:
    pool = tmp_path / "memory"
    pool.mkdir()
    (pool / "MEMORY.md").write_text("Setup prose only.\n", encoding="utf-8")
    _note(pool, "solo.md", "---\ntitle: Solo\n---\n")

    res = _run(tmp_path)

    assert res.returncode == 0, res.stderr
    assert (pool / "MEMORY.md").read_text(encoding="utf-8") == (
        "Setup prose only.\n\n## Pointers\n\n\n- [Solo](solo.md) \u2014 \n"
    )
