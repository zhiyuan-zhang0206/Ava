"""Contract tests for the manual hierarchy build entry (`scripts/build_hierarchy_once.py`).

The script composes five entry points: `load_known_texts` -> `build_agent_tree`
-> `write_tree`, plus the model lifecycle (`build_generation_llm` /
`close_chat_model`). These tests fake all five and lock the script's own
contract: dry-run builds but never writes; a clean run writes what was built
under the resolved model; the model is built once and closed even when the
build raises; failed nodes still let the rest write and force exit code 1; the
report shows levels, triggers and a bounded error list; and an empty build says
so instead of failing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from scripts import build_hierarchy_once as build
from shared.agents.history.hierarchy.generate import GenResult
from shared.agents.history.hierarchy.pipeline import MaterializedNode, MaterializedTree
from shared.config import settings


def _node(nid: str, level: int, trigger: str = "compact@i1") -> MaterializedNode:
    """One storage-shaped node; only the fields the report reads vary."""
    return MaterializedNode(
        nid=nid,
        level=level,
        kind="group",
        span=(0, 1),
        at=("2026-09-16T00:00:00+00:00", "2026-09-16T00:01:00+00:00"),
        trigger=trigger,
        src_tok=100,
        text="summary",
        text_hash="hash",
        input_hash="input",
        children=(),
        children_spans=(),
    )


def _tree(
    nodes: tuple[MaterializedNode, ...] = (),
    errors: tuple[GenResult, ...] = (),
    *,
    max_level: int = 1,
) -> MaterializedTree:
    return MaterializedTree(nodes=nodes, errors=errors, pending={}, max_level=max_level)


class _Recorder:
    """Captured interactions with the engine, the model and storage entry points."""

    def __init__(self) -> None:
        self.loaded: list[int] = []
        self.llm_models: list[str] = []
        self.llm_closes: list[Any] = []
        self.built: list[dict[str, Any]] = []
        self.written: list[dict[str, Any]] = []


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    tree: MaterializedTree,
    *,
    known: Mapping[str, str] | None = None,
    build_error: Exception | None = None,
) -> _Recorder:
    rec = _Recorder()

    def fake_load(agent_id: int) -> dict[str, str]:
        rec.loaded.append(agent_id)
        return dict(known or {})

    def fake_llm(model: str) -> Any:
        rec.llm_models.append(model)
        return object()

    def fake_close(llm: Any) -> None:
        rec.llm_closes.append(llm)

    def fake_build(
        agent_id: int, *, llm: Any, model: str, known_texts: Mapping[str, str] | None
    ) -> MaterializedTree:
        rec.built.append(
            {"agent_id": agent_id, "llm": llm, "model": model, "known_texts": known_texts}
        )
        if build_error is not None:
            raise build_error
        return tree

    def fake_write(agent_id: int, nodes: Sequence[MaterializedNode], *, model: str) -> int:
        rec.written.append({"agent_id": agent_id, "nodes": tuple(nodes), "model": model})
        return len(nodes)

    monkeypatch.setattr(build, "load_known_texts", fake_load)
    monkeypatch.setattr(build, "build_generation_llm", fake_llm)
    monkeypatch.setattr(build, "close_chat_model", fake_close)
    monkeypatch.setattr(build, "build_agent_tree", fake_build)
    monkeypatch.setattr(build, "write_tree", fake_write)
    return rec


def test_dry_run_builds_but_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rec = _install_fakes(monkeypatch, _tree((_node("L1#1", 1),)))

    rc = build.main(["--agent-id", "7", "--dry-run"])

    assert rc == 0
    assert rec.built and rec.built[0]["agent_id"] == 7
    assert rec.written == []
    out = capsys.readouterr().out
    assert "dry-run - nothing written" in out
    assert "upserted" not in out


def test_clean_run_writes_nodes_under_the_resolved_model(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    nodes = (_node("L1#1", 1), _node("L2#1", 2, trigger="tail"))
    rec = _install_fakes(monkeypatch, _tree(nodes, max_level=2))

    rc = build.main(["--agent-id", "7"])

    assert rc == 0
    assert rec.written[0]["agent_id"] == 7
    assert rec.written[0]["nodes"] == nodes
    assert rec.built[0]["model"] == settings.lm.hierarchy_model
    assert rec.llm_models == [settings.lm.hierarchy_model]
    assert rec.llm_closes == [rec.built[0]["llm"]]
    assert rec.written[0]["model"] == settings.lm.hierarchy_model
    assert "upserted 2 node row(s)" in capsys.readouterr().out


def test_model_flag_overrides_the_default_model(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _install_fakes(monkeypatch, _tree((_node("L1#1", 1),)))

    rc = build.main(["--agent-id", "7", "--model", "deepseek-v4-pro"])

    assert rc == 0
    assert rec.built[0]["model"] == "deepseek-v4-pro"
    assert rec.llm_models == ["deepseek-v4-pro"]
    assert rec.written[0]["model"] == "deepseek-v4-pro"


def test_known_cache_is_loaded_and_forwarded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rec = _install_fakes(monkeypatch, _tree((_node("L1#1", 1),)), known={"k": "v"})

    rc = build.main(["--agent-id", "9"])

    assert rc == 0
    assert rec.loaded == [9]
    assert rec.built[0]["known_texts"] == {"k": "v"}
    assert "generation cache: 1 known input(s)" in capsys.readouterr().out


def test_failed_nodes_still_write_and_return_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    errors = tuple(
        GenResult(nid=f"L1#{i}", src_tok=100, budget_tok=10, error="provider exploded")
        for i in range(1, 3)
    )
    rec = _install_fakes(monkeypatch, _tree((_node("L2#1", 2),), errors, max_level=2))

    rc = build.main(["--agent-id", "7"])

    assert rc == 1
    assert rec.written  # the partial tree still lands
    out = capsys.readouterr().out
    assert "errors: 2" in out
    assert "re-running retries them" in out


def test_error_report_shows_five_then_a_remainder(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    errors = tuple(
        GenResult(nid=f"n{i}", src_tok=10, budget_tok=1, error=f"e{i}") for i in range(7)
    )
    _install_fakes(monkeypatch, _tree(errors=errors))

    rc = build.main(["--agent-id", "1"])

    assert rc == 1
    out = capsys.readouterr().out
    assert "errors: 7" in out
    assert "  - n4: e4" in out  # the fifth shown line
    assert "  - n5" not in out  # beyond the cap
    assert "... and 2 more" in out


def test_empty_build_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_fakes(monkeypatch, _tree())

    rc = build.main(["--agent-id", "1"])

    assert rc == 0
    assert "nothing built" in capsys.readouterr().out


def test_model_closed_even_when_the_build_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _install_fakes(monkeypatch, _tree(), build_error=RuntimeError("engine exploded"))

    with pytest.raises(RuntimeError, match="engine exploded"):
        build.main(["--agent-id", "7"])

    assert rec.llm_models == [settings.lm.hierarchy_model]
    assert rec.llm_closes == [rec.built[0]["llm"]]
