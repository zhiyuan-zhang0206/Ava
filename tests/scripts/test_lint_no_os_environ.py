"""The raw-environment allowlist must describe live boundary crossings."""

from pathlib import Path

import pytest

from scripts import lint_no_os_environ


def test_raw_environment_exemptions_are_all_used() -> None:
    assert lint_no_os_environ._unused_file_exemptions() == []


def _unused_synthetic_exemption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> list[str]:
    rel_path = "synthetic_exemption.py"
    (tmp_path / rel_path).write_text(source, encoding="utf-8")
    monkeypatch.setattr(lint_no_os_environ, "_REPO_ROOT", tmp_path)
    monkeypatch.setattr(lint_no_os_environ, "_ALLOWED_FILES", frozenset({rel_path}))
    monkeypatch.setattr(lint_no_os_environ, "_GRANDFATHERED", frozenset[str]())
    return lint_no_os_environ._unused_file_exemptions()


def test_unused_raw_environment_exemption_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _unused_synthetic_exemption(tmp_path, monkeypatch, "VALUE = 1\n") == [
        "synthetic_exemption.py"
    ]


@pytest.mark.parametrize(
    "source",
    [
        '"""A one-line module docstring mentioning os.getenv()."""\nVALUE = 1\n',
        '''def example() -> int:
    """A multiline function docstring mentioning os.getenv().

    It contains no executable environment access.
    """
    return 1
''',
        '''class Example:
    """A multiline class docstring.

    It mentions os.environ only on its closing line."""
''',
    ],
    ids=("module-one-line", "function-multiline", "class-multiline"),
)
def test_docstring_only_mentions_leave_exemption_unused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    assert _unused_synthetic_exemption(tmp_path, monkeypatch, source) == ["synthetic_exemption.py"]


@pytest.mark.parametrize(
    "source",
    [
        'import os\nVALUE = os.getenv("AVA_EXAMPLE")\n',
        'import os\nVALUE = os.environ["AVA_EXAMPLE"]\n',
        '''import os
def example() -> str:
    """Mention os.getenv only in this docstring."""; return os.environ["AVA_EXAMPLE"]
''',
    ],
    ids=("getenv", "environ", "same-line-after-docstring"),
)
def test_real_raw_environment_use_keeps_exemption_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    assert _unused_synthetic_exemption(tmp_path, monkeypatch, source) == []


def test_generated_code_string_keeps_exemption_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = '''BOOTSTRAP = """
import os
VALUE = os.getenv("AVA_EXAMPLE")
"""
'''
    assert _unused_synthetic_exemption(tmp_path, monkeypatch, source) == []
