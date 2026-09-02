"""Contract tests for the daily official-model detector."""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "check_model_updates.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("check_model_updates", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_env_file(tracker: Any, path: Path, *, missing: str | None = None) -> None:
    path.write_text(
        "\n".join(
            f"{source.key_alias}=test-{source.provider}"
            for source in tracker.SOURCES.values()
            if source.key_alias != missing
        )
        + "\n"
    )


def _known_models(tracker: Any, source: Any) -> list[str]:
    return [
        model_id for model_id, spec in tracker.MODELS.items() if spec.provider == source.provider
    ][:1]


def _stub_fetcher(
    tracker: Any, overrides: dict[str, list[str]] | None = None
) -> Callable[[Any, str], list[str]]:
    def fetch(source: Any, api_key: str) -> list[str]:
        assert api_key == f"test-{source.provider}"
        if overrides is not None and source.provider in overrides:
            return overrides[source.provider]
        return _known_models(tracker, source)

    return fetch


def _missing_environment_value(_alias: str) -> None:
    return None


def test_tracker_script_exists() -> None:
    """The schedule's subprocess target must ship in the repository."""
    assert _SCRIPT.is_file()


def test_candidate_is_reported_once_then_recorded_in_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tracker = _load_script()
    env_file = tmp_path / ".env"
    _write_env_file(tracker, env_file)
    monkeypatch.setattr(tracker, "_environment_value", _missing_environment_value)
    # This id must stay unregistered; bump to the next GLM id when glm-5.4 lands.
    monkeypatch.setattr(
        tracker,
        "fetch_provider_models",
        _stub_fetcher(tracker, {"glm": ["glm-5.4"]}),
    )

    args = ["--env-file", str(env_file), "--state-dir", str(tmp_path / "state")]
    assert tracker.main(args) == 2
    assert "glm-5.4" in capsys.readouterr().out

    assert tracker.main(args) == 0
    assert "glm-5.4" not in capsys.readouterr().out.split("## Actionable candidates", 1)[-1]
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    assert state["providers"]["glm"]["reported"] == ["glm-5.4"]


def test_newer_same_series_is_actionable_while_older_upstream_member_is_suppressed() -> None:
    tracker = _load_script()
    registry = {
        "gemini-3.7-flash": type("Spec", (), {"provider": "gemini"})(),
    }

    comparison = tracker.compare_models(
        tracker.SOURCES["gemini"],
        ["gemini-3.6-flash", "gemini-3.8-flash"],
        registry,
    )

    assert comparison.candidates == ["gemini-3.8-flash"]
    assert comparison.suppressed == ["gemini-3.6-flash"]
    assert comparison.series_models["gemini-3.8-flash"] == ["gemini-3.7-flash"]


def test_qwen_major_only_variant_is_suppressed_but_same_version_variant_is_actionable() -> None:
    """Qwen variants without a minor version are older than the registered 3.8 head."""
    tracker = _load_script()
    registry = {
        "qwen3.8-max": type("Spec", (), {"provider": "qwen"})(),
    }

    comparison = tracker.compare_models(
        tracker.SOURCES["qwen"],
        ["qwen3-max-2025-09-23", "qwen3.8-2.4t-a95b"],
        registry,
    )

    assert comparison.candidates == ["qwen3.8-2.4t-a95b"]
    assert comparison.suppressed == ["qwen3-max-2025-09-23"]


def test_qwen_dated_snapshots_of_registered_aliases_are_suppressed() -> None:
    tracker = _load_script()
    registry = {
        "qwen3.8-max": type("Spec", (), {"provider": "qwen"})(),
    }

    comparison = tracker.compare_models(
        tracker.SOURCES["qwen"],
        [
            "qwen3.8-max-0902",
            "qwen3.8-max-2026-09-02",
            "qwen3.8-max-20260902",
            "qwen3.8-2.4t-a95b",
        ],
        registry,
    )

    assert comparison.candidates == ["qwen3.8-2.4t-a95b"]
    assert comparison.suppressed == [
        "qwen3.8-max-0902",
        "qwen3.8-max-2026-09-02",
        "qwen3.8-max-20260902",
    ]


def test_qwen_date_suffixes_with_unregistered_remainders_fall_through() -> None:
    tracker = _load_script()
    registry = {
        "qwen3.8-max": type("Spec", (), {"provider": "qwen"})(),
    }

    comparison = tracker.compare_models(
        tracker.SOURCES["qwen"],
        ["qwen3-max-2025-09-23", "qwen3.8-other-0902"],
        registry,
    )

    assert comparison.candidates == ["qwen3.8-other-0902"]
    assert comparison.suppressed == ["qwen3-max-2025-09-23"]


def test_openai_response_shape_drift_raises_instead_of_reading_no_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = _load_script()

    def malformed_response(
        url: str, *, headers: dict[str, str], params: dict[str, str | int]
    ) -> dict[str, str]:
        del url, headers, params
        return {"data": "not-a-list"}

    monkeypatch.setattr(
        tracker,
        "fetch_json",
        malformed_response,
    )

    with pytest.raises(TypeError, match="must be a list"):
        tracker.fetch_provider_models(tracker.SOURCES["gpt"], "test-key")


def test_missing_key_is_a_provider_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tracker = _load_script()
    env_file = tmp_path / ".env"
    _write_env_file(tracker, env_file, missing="GLM_API_KEY")
    monkeypatch.setattr(tracker, "_environment_value", _missing_environment_value)
    monkeypatch.setattr(tracker, "fetch_provider_models", _stub_fetcher(tracker))

    assert tracker.main(["--env-file", str(env_file), "--state-dir", str(tmp_path / "state")]) == 1
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    assert state["providers"]["glm"]["status"].startswith("error:")


@pytest.mark.parametrize("status", ["unknown", "error:"])
def test_state_rejects_statuses_outside_the_persisted_contract(tmp_path: Path, status: str) -> None:
    tracker = _load_script()
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"providers": {"glm": {"reported": [], "status": status}}}))

    with pytest.raises(ValueError, match="status"):
        tracker._load_state(state_path)


def test_qwen_envelope_paginates_until_total(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = _load_script()
    calls: list[dict[str, str | int]] = []
    first_page = [f"qwen3.8-model-{index}" for index in range(100)]
    responses = {
        1: {
            "output": {
                "models": [{"model": model_id} for model_id in first_page],
                "total": 101,
            }
        },
        2: {
            "output": {
                "models": [{"model": "qwen3.8-model-100"}],
                "total": 101,
            }
        },
    }

    def fetch_json(
        url: str, *, headers: dict[str, str], params: dict[str, str | int]
    ) -> dict[str, object]:
        assert headers["Authorization"] == "Bearer test-key"
        calls.append(params)
        return responses[params["page_no"]]  # type: ignore[index]

    monkeypatch.setattr(tracker, "fetch_json", fetch_json)

    assert tracker.fetch_provider_models(tracker.SOURCES["qwen"], "test-key") == [
        *first_page,
        "qwen3.8-model-100",
    ]
    assert calls == [
        {"page_no": 1, "page_size": 100},
        {"page_no": 2, "page_size": 100},
    ]


def test_qwen_total_must_match_the_collected_models(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = _load_script()

    def short_response(
        url: str, *, headers: dict[str, str], params: dict[str, str | int]
    ) -> dict[str, object]:
        del url, headers, params
        return {"output": {"models": [], "total": 1}}

    monkeypatch.setattr(
        tracker,
        "fetch_json",
        short_response,
    )

    with pytest.raises(ValueError, match="total does not match"):
        tracker.fetch_provider_models(tracker.SOURCES["qwen"], "test-key")


def test_status_change_notifies_once_before_returning_to_regular_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tracker = _load_script()
    env_file = tmp_path / ".env"
    _write_env_file(tracker, env_file)
    monkeypatch.setattr(tracker, "_environment_value", _missing_environment_value)
    monkeypatch.setattr(tracker, "fetch_provider_models", _stub_fetcher(tracker))
    args = ["--env-file", str(env_file), "--state-dir", str(tmp_path / "state")]

    assert tracker.main(args) == 0
    _write_env_file(tracker, env_file, missing="XAI_API_KEY")
    assert tracker.main(args) == 2
    assert tracker.main(args) == 1


def test_empty_fetch_error_still_produces_a_valid_persisted_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tracker = _load_script()
    env_file = tmp_path / ".env"
    _write_env_file(tracker, env_file)
    monkeypatch.setattr(tracker, "_environment_value", _missing_environment_value)

    def fetch(source: Any, api_key: str) -> list[str]:
        if source.provider == "glm":
            raise ValueError
        return _known_models(tracker, source)

    monkeypatch.setattr(tracker, "fetch_provider_models", fetch)
    args = ["--env-file", str(env_file), "--state-dir", str(tmp_path / "state")]

    assert tracker.main(args) == 1
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    assert state["providers"]["glm"]["status"] == "error: ValueError"
    assert tracker.main(args) == 1


def test_write_report_persists_markdown_and_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tracker = _load_script()
    env_file = tmp_path / ".env"
    report_dir = tmp_path / "reports"
    _write_env_file(tracker, env_file)
    monkeypatch.setattr(tracker, "_environment_value", _missing_environment_value)
    monkeypatch.setattr(tracker, "fetch_provider_models", _stub_fetcher(tracker))

    assert (
        tracker.main(
            [
                "--env-file",
                str(env_file),
                "--state-dir",
                str(tmp_path / "state"),
                "--write-report",
                str(report_dir),
            ]
        )
        == 0
    )
    assert "## Actionable candidates" in (report_dir / "last-report.md").read_text()
    assert json.loads((report_dir / "last-report.json").read_text())["providers"]
