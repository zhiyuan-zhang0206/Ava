"""`scripts/lint_ava_okf.py` — a typo'd explicit target must fail the gate.

An explicit path argument that does not exist used to print
"No .ava.okf.md files found." and exit 0; it must now report the missing
target on stderr and exit 1.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from scripts import lint_ava_okf as gate


def test_explicit_missing_target_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo'd explicit path must fail, not exit 0 with a confusing message."""
    good = tmp_path / "ok.ava.okf.md"
    good.write_text("# placeholder\n", encoding="utf-8")
    missing = tmp_path / "typo.ava.okf.md"
    monkeypatch.setattr(sys, "argv", ["lint_ava_okf.py", str(missing)])
    with pytest.raises(SystemExit) as exc:
        gate.main()
    assert exc.value.code == 1
    assert str(missing) in capsys.readouterr().err
    monkeypatch.setattr(sys, "argv", ["lint_ava_okf.py", str(good), str(missing)])
    with pytest.raises(SystemExit) as exc2:
        gate.main()
    assert exc2.value.code == 1
