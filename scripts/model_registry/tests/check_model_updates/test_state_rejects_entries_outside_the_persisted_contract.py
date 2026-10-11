"""Check model updates cases: state rejects entries outside the persisted contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.model_registry.tests.test_check_model_updates import (
    _known_models,
    _missing_environment_value,
    _stub_fetcher,
    _write_env_file,
)
from scripts.model_registry.tests.test_check_model_updates import (
    test_fetch_json_raises_after_the_retry_budget_is_exhausted as test_fetch_json_raises_after_the_retry_budget_is_exhausted,
)
from scripts.model_registry.tests.test_check_model_updates import (
    test_fetch_json_retries_a_transient_connection_error as test_fetch_json_retries_a_transient_connection_error,
)
from scripts.model_registry.tests.test_check_model_updates import (
    test_permanent_http_error_is_not_retried as test_permanent_http_error_is_not_retried,
)
from scripts.model_registry.tests.test_check_model_updates import (
    test_repeated_provider_failure_announces_once_and_never_repeats as test_repeated_provider_failure_announces_once_and_never_repeats,
)
from scripts.model_registry.tests.test_check_model_updates import (
    test_transient_ssl_eof_is_retried_without_marking_the_provider_error as test_transient_ssl_eof_is_retried_without_marking_the_provider_error,
)


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ({"reported": [], "status": "unknown"}, "status"),
        ({"reported": [], "status": "error:"}, "status"),
        ({"reported": [], "status": "ok", "announced": "unknown"}, "status"),
        ({"reported": [], "status": "ok", "pending": {"status": "ok"}}, "pending"),
        ({"reported": [], "status": "ok", "pending": {"status": "ok", "rounds": 0}}, "rounds"),
    ],
)
def test_state_rejects_entries_outside_the_persisted_contract(
    tmp_path: Path, entry: dict[str, object], match: str, bare_tracker: Any
) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"providers": {"glm": entry}}))

    with pytest.raises((TypeError, ValueError), match=match):
        bare_tracker._load_state(state_path)


def test_qwen_envelope_paginates_until_total(
    monkeypatch: pytest.MonkeyPatch, bare_tracker: Any
) -> None:
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

    monkeypatch.setattr(bare_tracker, "fetch_json", fetch_json)

    assert bare_tracker.fetch_provider_models(bare_tracker.SOURCES["qwen"], "test-key") == [
        *first_page,
        "qwen3.8-model-100",
    ]
    assert calls == [
        {"page_no": 1, "page_size": 100},
        {"page_no": 2, "page_size": 100},
    ]


def test_qwen_total_must_match_the_collected_models(
    monkeypatch: pytest.MonkeyPatch, bare_tracker: Any
) -> None:

    def short_response(
        url: str, *, headers: dict[str, str], params: dict[str, str | int]
    ) -> dict[str, object]:
        del url, headers, params
        return {"output": {"models": [], "total": 1}}

    monkeypatch.setattr(
        bare_tracker,
        "fetch_json",
        short_response,
    )

    with pytest.raises(ValueError, match="total does not match"):
        bare_tracker.fetch_provider_models(bare_tracker.SOURCES["qwen"], "test-key")


def test_single_round_transient_error_and_its_recovery_stay_silent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bare_tracker: Any
) -> None:
    """2026-09-14: one SSL EOF towards gemini announced an error, and the next
    morning's recovery announced a second time. A one-round blip must stay
    silent on both sides: its pending record clears without anything having
    been announced."""
    env_file = tmp_path / ".env"
    _write_env_file(bare_tracker, env_file)
    monkeypatch.setattr(bare_tracker, "_environment_value", _missing_environment_value)
    monkeypatch.setattr(bare_tracker, "fetch_provider_models", _stub_fetcher(bare_tracker))
    args = ["--env-file", str(env_file), "--state-dir", str(tmp_path / "state")]

    assert bare_tracker.main(args) == 0
    _write_env_file(bare_tracker, env_file, missing="GEMINI_API_KEY")
    assert bare_tracker.main(args) == 0
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    assert state["providers"]["gemini"]["pending"]["rounds"] == 1
    assert state["providers"]["gemini"]["announced"] == "ok"

    _write_env_file(bare_tracker, env_file)
    assert bare_tracker.main(args) == 0
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    assert state["providers"]["gemini"]["status"] == "ok"
    assert "pending" not in state["providers"]["gemini"]


def test_persistent_status_change_announces_once_and_recovery_announces_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bare_tracker: Any
) -> None:
    env_file = tmp_path / ".env"
    _write_env_file(bare_tracker, env_file)
    monkeypatch.setattr(bare_tracker, "_environment_value", _missing_environment_value)
    monkeypatch.setattr(bare_tracker, "fetch_provider_models", _stub_fetcher(bare_tracker))
    args = ["--env-file", str(env_file), "--state-dir", str(tmp_path / "state")]

    assert bare_tracker.main(args) == 0
    _write_env_file(bare_tracker, env_file, missing="XAI_API_KEY")
    assert bare_tracker.main(args) == 0  # first failing round: pending, not announced
    assert bare_tracker.main(args) == 2  # second failing round: announced once
    assert bare_tracker.main(args) == 0  # a continuing error never re-announces
    _write_env_file(bare_tracker, env_file)
    assert bare_tracker.main(args) == 0  # the first healthy round is pending too
    assert bare_tracker.main(args) == 2  # the recovery is announced
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    assert state["providers"]["grok"]["status"] == "ok"
    assert state["providers"]["grok"]["announced"] == "ok"
    assert "pending" not in state["providers"]["grok"]


def test_legacy_state_upgrade_baselines_the_loaded_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bare_tracker: Any
) -> None:
    """A state file written before `announced` existed (like a live one
    left in error by the 2026-09-16 proxy outage) baselines on the status
    it was left in: the upgrade itself announces nothing, and a resolution
    still walks the two-round window."""
    env_file = tmp_path / ".env"
    _write_env_file(bare_tracker, env_file)
    monkeypatch.setattr(bare_tracker, "_environment_value", _missing_environment_value)
    monkeypatch.setattr(bare_tracker, "fetch_provider_models", _stub_fetcher(bare_tracker))
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps({"providers": {"gemini": {"reported": [], "status": "error: old outage"}}})
    )
    args = ["--env-file", str(env_file), "--state-dir", str(state_dir)]

    assert bare_tracker.main(args) == 0  # the first healthy round is pending, silent
    state = json.loads((state_dir / "state.json").read_text())
    assert state["providers"]["gemini"]["announced"] == "error: old outage"
    assert bare_tracker.main(args) == 2  # the second announces the recovery
    state = json.loads((state_dir / "state.json").read_text())
    assert state["providers"]["gemini"]["announced"] == "ok"
    assert "pending" not in state["providers"]["gemini"]


def test_empty_fetch_error_still_produces_a_valid_persisted_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bare_tracker: Any
) -> None:
    env_file = tmp_path / ".env"
    _write_env_file(bare_tracker, env_file)
    monkeypatch.setattr(bare_tracker, "_environment_value", _missing_environment_value)

    def fetch(source: Any, api_key: str) -> list[str]:
        if source.provider == "glm":
            raise ValueError
        return _known_models(bare_tracker, source)

    monkeypatch.setattr(bare_tracker, "fetch_provider_models", fetch)
    args = ["--env-file", str(env_file), "--state-dir", str(tmp_path / "state")]

    assert bare_tracker.main(args) == 0
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    assert state["providers"]["glm"]["status"] == "error: ValueError"
    assert bare_tracker.main(args) == 2


def test_write_report_persists_markdown_and_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bare_tracker: Any
) -> None:
    env_file = tmp_path / ".env"
    report_dir = tmp_path / "reports"
    _write_env_file(bare_tracker, env_file)
    monkeypatch.setattr(bare_tracker, "_environment_value", _missing_environment_value)
    monkeypatch.setattr(bare_tracker, "fetch_provider_models", _stub_fetcher(bare_tracker))

    assert (
        bare_tracker.main(
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
