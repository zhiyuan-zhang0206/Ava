"""The import cache reads changed files and retains unchanged normalized text."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.structure.imports import cache
from scripts.structure.tests.patch_repo import make_repo, write


def test_an_unchanged_file_is_not_read_again_and_a_changed_one_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_repo(tmp_path, {"cli/commands/run.py": "from base.net import retry\n"})
    tops = ("base", "ava", "cli")
    reads: list[str] = []
    real = cache._statements

    def counting(path: Path, rel: str, scope: tuple[str, ...]) -> str:
        reads.append(rel)
        return real(path, rel, scope)

    monkeypatch.setattr(cache, "_statements", counting)
    cold = cache.production_imports(root, tops)
    assert len(reads) == len(cold)

    reads.clear()
    assert cache.production_imports(root, tops) == cold
    assert reads == []

    write(root, "cli/commands/run.py", "from base.db import pool\n")
    assert cache.production_imports(root, tops)["cli/commands/run.py"] == "from base.db import pool"
    assert reads == ["cli/commands/run.py"]

    reads.clear()
    (root / "cli/commands/_util.py").unlink()
    assert "cli/commands/_util.py" not in cache.production_imports(root, tops)
    assert reads == []
