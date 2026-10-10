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
    real = Path.read_text

    def counting(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if path.is_relative_to(root) and path.suffix == ".py":
            reads.append(path.relative_to(root).as_posix())
        return real(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", counting)
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
