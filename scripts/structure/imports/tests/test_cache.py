from __future__ import annotations

from pathlib import Path

from scripts.structure.imports import cache
from scripts.structure.tests.patch_repo import make_repo, write


def test_an_unchanged_file_is_not_read_again_and_a_changed_one_is(tmp_path: Path) -> None:
    root = make_repo(tmp_path, {"cli/commands/run.py": "from base.net import retry\n"})
    tops = ("base", "meiva", "cli")
    reads: list[str] = []

    def counting(path: Path) -> str:
        reads.append(path.relative_to(root).as_posix())
        return path.read_text(encoding="utf-8")

    cold = cache.production_imports(root, tops, read_source=counting)
    assert len(reads) == len(cold)

    reads.clear()
    assert cache.production_imports(root, tops, read_source=counting) == cold
    assert reads == []

    write(root, "cli/commands/run.py", "from base.db import pool\n")
    assert (
        cache.production_imports(root, tops, read_source=counting)["cli/commands/run.py"]
        == "from base.db import pool"
    )
    assert reads == ["cli/commands/run.py"]

    reads.clear()
    (root / "cli/commands/_util.py").unlink()
    assert "cli/commands/_util.py" not in cache.production_imports(root, tops, read_source=counting)
    assert reads == []
