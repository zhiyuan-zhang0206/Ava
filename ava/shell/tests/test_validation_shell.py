"""Argument validation of the `ava.shell` entry points."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import ava
import ava.sdk_surface.agent_identity
from ava import shell as _shell


class TestShellEntries:
    def test_run_cmd_unwraps(self) -> None:
        result = _shell.run(("echo sdk-validation-smoke",))  # pyright: ignore[reportArgumentType]
        assert "sdk-validation-smoke" in str(result)

    def test_run_multi_element_cmd_type_errors(self) -> None:
        with pytest.raises(TypeError, match="cmd must be a string"):
            _shell.run(("echo a", "echo b"))  # pyright: ignore[reportArgumentType]

    def test_run_background_normalizes_and_starts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        created: dict[str, Any] = {}
        sent: dict[str, Any] = {}
        monkeypatch.setattr(
            _shell.sessions,
            "create_session",
            lambda name, **kw: created.update(name=name, **kw) or (42, "full"),  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(
            _shell.background,
            "allocate_output_path",
            lambda sid, _name: tmp_path / f"{sid}.log",  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(_shell.background, "notified_line", lambda *_a, **_kw: "line")  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(
            _shell.sessions,
            "send",
            lambda sid, line: sent.update(sid=sid, line=line),  # pyright: ignore[reportUnknownArgumentType]
        )  # pyright: ignore[reportUnknownArgumentType]
        monkeypatch.setattr(ava.sdk_surface.agent_identity, "agent_id", lambda: 900001)

        run = _shell.run_background(("echo hi",), name=("bg",), ttl=60)  # pyright: ignore[reportArgumentType]
        assert run.session_id == 42
        assert created["name"] == "bg"
        assert sent["line"] == f". {tmp_path / '42.sh'}"
        assert (tmp_path / "42.sh").read_text().endswith("\nline\n")

    def test_run_background_multi_element_name_type_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(TypeError, match="name must be a string"):
            _shell.run_background("echo hi", name=("a", "b"), ttl=60)  # pyright: ignore[reportArgumentType]
