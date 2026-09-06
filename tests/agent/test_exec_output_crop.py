"""Soft output previews keep recoverable files while their context references live."""

from pathlib import Path
from typing import IO, Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError

from agent.graph import _exec_output
from shared.config import settings
from shared.config.sandbox import SandboxSettings


@pytest.fixture
def archive_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / ".exec_output"
    monkeypatch.setattr(_exec_output, "_overflow_dir", lambda: directory)
    return directory


def _output(count: int = 140) -> str:
    return "".join(f"line {index:03d} {'content ' * 12}\n" for index in range(count))


def test_default_crop_keeps_25_lines_at_each_end_and_recoverable_body(archive_dir: Path):
    body = _output()
    wrapped = _exec_output.wrap_code_output(body)

    assert "line 024 " in wrapped
    assert "line 025 " not in wrapped
    assert "line 114 " not in wrapped
    assert "line 115 " in wrapped
    assert "line 139 " in wrapped
    files = list(archive_dir.glob("crop_*.txt"))
    assert len(files) == 1
    assert str(files[0]) in wrapped
    assert files[0].read_text() == body
    assert len(wrapped) < len(body)


def test_context_reference_survives_legacy_ring_churn(archive_dir: Path):
    body = _output()
    wrapped = _exec_output.wrap_code_output(body)
    archive = next(archive_dir.glob("crop_*.txt"))
    for _ in range(25):
        _exec_output.wrap_code_output("x" * 31_000)
    assert len(list(archive_dir.glob("exec_*.txt"))) == 20
    assert archive.read_text() == body
    assert str(archive) in wrapped


def test_referenced_archive_is_kept_when_budget_is_full(
    archive_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    body = _output()
    monkeypatch.setattr(settings.sandbox, "exec_output_crop_archive_max_bytes", len(body.encode()))
    first = _exec_output.wrap_code_output(body)
    archive = next(archive_dir.glob("crop_*.txt"))
    second_body = body.replace("content", "another")
    second = _exec_output.wrap_code_output(
        second_body, referenced_messages=[HumanMessage(content=first)]
    )
    assert second_body in second
    assert archive.read_text() == body
    assert list(archive_dir.glob("crop_*.txt")) == [archive]


def test_unreferenced_archive_is_evicted_under_byte_budget(
    archive_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    body = _output()
    monkeypatch.setattr(settings.sandbox, "exec_output_crop_archive_max_bytes", len(body.encode()))
    _exec_output.wrap_code_output(body)
    old = next(archive_dir.glob("crop_*.txt"))
    second_body = body.replace("content", "changed")
    second = _exec_output.wrap_code_output(second_body)
    assert not old.exists()
    files = list(archive_dir.glob("crop_*.txt"))
    assert len(files) == 1
    assert files[0].read_text() == second_body
    assert str(files[0]) in second


def test_execute_code_argument_reference_protects_archive(
    archive_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    body = _output()
    monkeypatch.setattr(settings.sandbox, "exec_output_crop_archive_max_bytes", len(body.encode()))
    _exec_output.wrap_code_output(body)
    archive = next(archive_dir.glob("crop_*.txt"))
    call = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "execute_code",
                "args": {"code": f"ava.files.read({str(archive)!r})"},
                "id": "read",
            }
        ],
    )
    wrapped = _exec_output.wrap_code_output(body, referenced_messages=[call])
    assert archive.exists()
    assert body in wrapped


@pytest.mark.parametrize("kind", ["thinking", "reasoning", "provider_reasoning"])
def test_reasoning_reference_protects_archive(
    archive_dir: Path, monkeypatch: pytest.MonkeyPatch, kind: str
):
    body = _output()
    monkeypatch.setattr(settings.sandbox, "exec_output_crop_archive_max_bytes", len(body.encode()))
    _exec_output.wrap_code_output(body)
    archive = next(archive_dir.glob("crop_*.txt"))
    if kind == "provider_reasoning":
        message = AIMessage(content="", additional_kwargs={"reasoning_content": str(archive)})
    elif kind == "reasoning":
        message = AIMessage(
            content=[
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": str(archive)}]}
            ]
        )
    else:
        message = AIMessage(content=[{"type": "thinking", "thinking": str(archive)}])
    wrapped = _exec_output.wrap_code_output(body, referenced_messages=[message])
    assert archive.exists()
    assert body in wrapped


@pytest.mark.parametrize("body", [_output(120), "\n" * 121, "x" * 12_000])
def test_threshold_short_lines_and_single_line_do_not_create_archive(body: str, archive_dir: Path):
    assert body in _exec_output.wrap_code_output(body)
    assert not archive_dir.exists()


def test_zero_threshold_disables_soft_crop_only(archive_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings.sandbox, "exec_output_crop_after_lines", 0)
    assert _output() in _exec_output.wrap_code_output(_output())
    wrapped = _exec_output.wrap_code_output("x" * 31_000)
    assert "output truncated" in wrapped
    assert list(archive_dir.glob("exec_*.txt"))
    assert not list(archive_dir.glob("crop_*.txt"))


def test_newline_spelling_and_unterminated_tail_survive(archive_dir: Path):
    body = _output().replace("\n", "\r\n").removesuffix("\r\n")
    wrapped = _exec_output.wrap_code_output(body)
    assert body.splitlines(keepends=True)[0] in wrapped
    assert body.splitlines(keepends=True)[-1] in wrapped
    assert next(archive_dir.glob("crop_*.txt")).read_bytes() == body.encode()


def test_budget_counts_utf8_bytes(archive_dir: Path, monkeypatch: pytest.MonkeyPatch):
    body = _output().replace("content", "\u6d4b\u8bd5\u8f93\u51fa\u7ed3\u679c")
    monkeypatch.setattr(settings.sandbox, "exec_output_crop_archive_max_bytes", len(body))
    assert body in _exec_output.wrap_code_output(body)
    assert not archive_dir.exists()


def test_archive_write_failure_keeps_body_without_false_recovery_path(
    archive_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    original = Path.open

    def fail_archive_write(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> IO[Any]:
        if path.name.startswith("crop_"):
            raise OSError("test archive storage unavailable")
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_archive_write)
    body = _output()
    assert body in _exec_output.wrap_code_output(body)
    assert not list(archive_dir.glob("crop_*.txt"))


def test_head_tail_counts_are_independent_of_trigger(
    archive_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings.sandbox, "exec_output_crop_after_lines", 130)
    monkeypatch.setattr(settings.sandbox, "exec_output_crop_head_lines", 3)
    monkeypatch.setattr(settings.sandbox, "exec_output_crop_tail_lines", 2)
    wrapped = _exec_output.wrap_code_output(_output())
    assert "line 002 " in wrapped and "line 003 " not in wrapped
    assert "line 137 " not in wrapped and "line 138 " in wrapped
    assert "first 3 + last 2 lines" in wrapped


def test_failed_archive_cleanup_cannot_drop_original_tool_output(
    archive_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    def fail_chmod(path: Path, mode: int) -> None:
        raise OSError("test archive permissions unavailable")

    def fail_unlink(path: Path, missing_ok: bool = False) -> None:
        raise OSError("test archive cleanup unavailable")

    monkeypatch.setattr(Path, "chmod", fail_chmod)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    body = _output()
    wrapped = _exec_output.wrap_code_output(body)
    assert body in wrapped
    assert "full output at" not in wrapped
    assert next(archive_dir.glob("crop_*.txt")).read_bytes() == b""


@pytest.mark.parametrize(
    "field,value",
    [
        ("exec_output_crop_after_lines", -1),
        ("exec_output_crop_head_lines", 0),
        ("exec_output_crop_tail_lines", 0),
        ("exec_output_crop_archive_max_bytes", 0),
    ],
)
def test_invalid_crop_config_is_rejected(field: str, value: int):
    with pytest.raises(ValidationError):
        SandboxSettings.model_validate({field: value})


def test_config_defaults_and_disabled_trigger():
    config = SandboxSettings.model_validate({})
    assert config.exec_output_crop_after_lines == 120
    assert config.exec_output_crop_head_lines == config.exec_output_crop_tail_lines == 25
    assert config.exec_output_crop_archive_max_bytes == 16 * 1024 * 1024
    assert (
        SandboxSettings.model_validate(
            {"exec_output_crop_after_lines": 0}
        ).exec_output_crop_after_lines
        == 0
    )
