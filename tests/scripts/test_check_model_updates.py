"""Contract tests for the daily official-model detector."""

from __future__ import annotations

import importlib.util
import json
import socket
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


class _JSONResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> object:
        return self._payload


def test_tracker_script_exists() -> None:
    """The schedule's subprocess target must ship in the repository."""
    assert _SCRIPT.is_file()


def test_fake_ip_range_boundaries() -> None:
    tracker = _load_script()

    assert tracker._is_fake_ip("198.18.0.0")
    assert tracker._is_fake_ip("198.19.255.255")
    assert not tracker._is_fake_ip("198.17.255.255")
    assert not tracker._is_fake_ip("198.20.0.0")
    assert not tracker._is_fake_ip("104.18.6.192")
    assert not tracker._is_fake_ip("not-an-ip")


def test_doh_real_ip_falls_back_to_second_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = _load_script()
    host = "api.example.com"
    calls: list[str] = []

    def get(
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str],
        timeout: int,
    ) -> _JSONResponse:
        assert headers == {"accept": "application/dns-json"}
        assert params == {"name": host, "type": "A"}
        assert timeout == 10
        calls.append(url)
        if url == tracker._DOH_ENDPOINTS[0]:
            raise tracker.requests.exceptions.ConnectionError("primary unavailable")
        return _JSONResponse({"Answer": [{"name": host, "type": 1, "data": "104.18.1.2"}]})

    monkeypatch.setattr(tracker.requests, "get", get)

    assert tracker._doh_real_ip(host) == "104.18.1.2"
    assert calls == list(tracker._DOH_ENDPOINTS)


def test_doh_real_ip_skips_fake_ip_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = _load_script()
    host = "api.example.com"
    calls: list[str] = []
    addresses = iter(["198.18.0.5", "104.18.1.2"])

    def get(
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str],
        timeout: int,
    ) -> _JSONResponse:
        del headers, params, timeout
        calls.append(url)
        return _JSONResponse({"Answer": [{"name": host, "type": 1, "data": next(addresses)}]})

    monkeypatch.setattr(tracker.requests, "get", get)

    assert tracker._doh_real_ip(host) == "104.18.1.2"
    assert calls == list(tracker._DOH_ENDPOINTS)


def test_doh_real_ip_raises_when_all_endpoints_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = _load_script()
    calls: list[str] = []

    def get(url: str, **_kwargs: object) -> _JSONResponse:
        calls.append(url)
        raise tracker.requests.exceptions.ConnectionError("unavailable")

    monkeypatch.setattr(tracker.requests, "get", get)

    with pytest.raises(ConnectionError, match="DoH"):
        tracker._doh_real_ip("api.example.com")
    assert calls == list(tracker._DOH_ENDPOINTS)


def test_doh_real_ip_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = _load_script()
    host = "api.example.com"
    calls: list[str] = []

    def get(url: str, **_kwargs: object) -> _JSONResponse:
        calls.append(url)
        return _JSONResponse({"Answer": [{"name": host, "type": 1, "data": "104.18.1.2"}]})

    monkeypatch.setattr(tracker.requests, "get", get)

    assert tracker._doh_real_ip(host) == "104.18.1.2"
    assert tracker._doh_real_ip(host) == "104.18.1.2"
    assert calls == [tracker._DOH_ENDPOINTS[0]]


def test_session_for_host_healthy_resolution_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = _load_script()
    host = "api.example.com"

    def getaddrinfo(
        _host: str, _port: int, *, type: socket.SocketKind
    ) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
        del type
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.18.1.2", 443))]

    monkeypatch.setattr(
        tracker.socket,
        "getaddrinfo",
        getaddrinfo,
    )

    assert tracker._session_for_host(host) is None


def test_session_for_host_fake_ip_returns_pinned_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = _load_script()
    host = "api.example.com"
    pinned_ip = "140.82.113.3"

    def getaddrinfo(
        _host: str, _port: int, *, type: socket.SocketKind
    ) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
        del type
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.2.25", 443))]

    def doh_real_ip(_host: str) -> str:
        return pinned_ip

    monkeypatch.setattr(
        tracker.socket,
        "getaddrinfo",
        getaddrinfo,
    )
    monkeypatch.setattr(tracker, "_doh_real_ip", doh_real_ip)

    session = tracker._session_for_host(host)

    assert isinstance(session, tracker.requests.Session)
    adapter = session.get_adapter(f"https://{host}/")
    assert isinstance(adapter, tracker.PinnedIPHTTPSAdapter)
    assert adapter._pinned_ip == pinned_ip
    prepared = tracker.requests.Request("GET", f"https://{host}/models").prepare()
    pool = adapter.get_connection_with_tls_context(prepared, verify=True)
    connection = pool._new_conn()
    try:
        assert connection._dns_host == pinned_ip
        assert connection.host == host
    finally:
        connection.close()


def test_fetch_json_uses_pinned_session_when_fake_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = _load_script()
    url = "https://api.example.com/models"
    headers = {"Authorization": "Bearer test-key"}
    params = {"limit": 100}
    payload: dict[str, object] = {"data": []}
    session_calls: list[tuple[str, dict[str, object]]] = []
    plain_calls: list[tuple[str, dict[str, object]]] = []

    class Session:
        def get(self, request_url: str, **kwargs: object) -> _JSONResponse:
            session_calls.append((request_url, kwargs))
            return _JSONResponse(payload)

    session = Session()

    def session_for_host(_host: str) -> Session:
        return session

    monkeypatch.setattr(tracker, "_session_for_host", session_for_host)

    assert tracker.fetch_json(url, headers=headers, params=params) == payload
    assert session_calls == [
        (
            url,
            {
                "headers": {"User-Agent": tracker._USER_AGENT, **headers},
                "params": params,
                "timeout": tracker._TIMEOUT_SECONDS,
            },
        )
    ]

    def plain_get(request_url: str, **kwargs: object) -> _JSONResponse:
        plain_calls.append((request_url, kwargs))
        return _JSONResponse(payload)

    def no_session_for_host(_host: str) -> None:
        return None

    monkeypatch.setattr(tracker, "_session_for_host", no_session_for_host)
    monkeypatch.setattr(tracker.requests, "get", plain_get)

    assert tracker.fetch_json(url, headers=headers, params=params) == payload
    assert plain_calls == session_calls


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


def test_gpt6_family_ids_are_classified_not_dropped_as_other() -> None:
    """gpt-6-* must stay inside the gpt family patterns: a registered gpt-6
    member is skipped, and a same-series gpt-6 variant compares against the
    5.x registry head (older major) and stays actionable."""
    tracker = _load_script()
    registry = {
        "gpt-6-astra": type("Spec", (), {"provider": "gpt"})(),
        "gpt-5.6-sol": type("Spec", (), {"provider": "gpt"})(),
    }

    comparison = tracker.compare_models(
        tracker.SOURCES["gpt"],
        ["gpt-6-astra", "gpt-6-sol"],
        registry,
    )

    assert comparison.candidates == ["gpt-6-sol"]
    assert comparison.suppressed == []
    assert comparison.other_ids == []
    assert comparison.series_models["gpt-6-sol"] == ["gpt-5.6-sol"]


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
