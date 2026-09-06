#!/usr/bin/env python3
"""Fail-closed daily comparison of official provider models and Ava's registry."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, cast, override
from urllib.parse import urlparse

import requests
from dotenv import dotenv_values
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.poolmanager import PoolManager

from shared.lm.registry import MODELS
from shared.paths import ava_home
from shared.runtime_config import read_env_aliases

_USER_AGENT = "Ava model-update tracker"
_TIMEOUT_SECONDS, _QWEN_PAGE_SIZE = 30, 100
_DOH_TIMEOUT_SECONDS = 10
_DOH_ENDPOINTS = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
)
_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_doh_cache: dict[str, str] = {}
_DATED_SNAPSHOT_SUFFIX = re.compile(r"-\d{4}(?:-\d{2}(?:-\d{2})?)?$|-\d{8}$")


@dataclass(frozen=True)
class SourceDescriptor:
    """Official model-list endpoint plus the roster family Ava evaluates."""

    provider: str
    base_url: str
    models_path: str
    auth_header: str | None
    key_alias: str
    response_kind: Literal["openai", "anthropic", "gemini", "dashscope"]
    family_pattern: re.Pattern[str]
    series_pattern: re.Pattern[str]
    version_pattern: re.Pattern[str]
    headers: tuple[tuple[str, str], ...] = ()


# fmt: off
SOURCES: dict[str, SourceDescriptor] = {
    # Official docs: DeepSeek Models API.
    "deepseek": SourceDescriptor("deepseek", "https://api.deepseek.com", "/models", "Authorization", "DEEPSEEK_API_KEY", "openai", re.compile(r"^deepseek-v\d+-(flash|pro)$"),
                                 re.compile(r"^deepseek-v\d+-(?P<series>flash|pro)$"), re.compile(r"^deepseek-v(?P<version>\d+)-(flash|pro)$")),
    # Official docs: OpenAI Models API.
    "gpt": SourceDescriptor("gpt", "https://api.openai.com", "/v1/models", "Authorization", "OPENAI_API_KEY", "openai", re.compile(r"^gpt-(?:5\.\d+|6)(?:-(?:sol|terra|luna|astra))?$"),
                            re.compile(r"^gpt-(?:5\.\d+|6)(?:-(?P<series>sol|terra|luna|astra))?$"), re.compile(r"^gpt-(?P<version>5\.\d+|6)(?:-(?:sol|terra|luna|astra))?$")),
    # Official docs: Anthropic Models API.
    "claude": SourceDescriptor("claude", "https://api.anthropic.com", "/v1/models", "x-api-key", "ANTHROPIC_API_KEY", "anthropic", re.compile(r"^claude-(opus|sonnet|haiku|fable)-\d"),
                               re.compile(r"^claude-(?P<series>opus|sonnet|haiku|fable)-\d"), re.compile(r"^claude-(opus|sonnet|haiku|fable)-(?P<version>\d+(?:-\d+)?)"), (("anthropic-version", "2023-06-01"),)),
    # Official docs: Gemini Models API.
    "gemini": SourceDescriptor("gemini", "https://generativelanguage.googleapis.com", "/v1beta/models", None, "GEMINI_API_KEY", "gemini", re.compile(r"^gemini-\d+(\.\d+)?-(pro|flash)(-preview)?$"),
                               re.compile(r"^gemini-\d+(?:\.\d+)?-(?P<series>pro|flash)(?P<preview>-preview)?$"), re.compile(r"^gemini-(?P<version>\d+(?:\.\d+)?)-(pro|flash)(-preview)?$")),
    # Official docs: xAI Models API.
    "grok": SourceDescriptor("grok", "https://api.x.ai", "/v1/models", "Authorization", "XAI_API_KEY", "openai", re.compile(r"^grok-\d+\.\d+$"),
                             re.compile(r"^(?P<series>grok)-\d+\.\d+$"), re.compile(r"^grok-(?P<version>\d+\.\d+)$")),
    # Official docs: Moonshot Models API.
    "kimi": SourceDescriptor("kimi", "https://api.moonshot.ai", "/v1/models", "Authorization", "MOONSHOT_API_KEY", "openai", re.compile(r"^kimi-k\d"),
                             re.compile(r"^(?P<series>kimi)-k\d"), re.compile(r"^kimi-k(?P<version>\d+(?:\.\d+)?)")),
    # Official docs: Zhipu AI Models API.
    "glm": SourceDescriptor("glm", "https://open.bigmodel.cn", "/api/paas/v4/models", "Authorization", "GLM_API_KEY", "openai", re.compile(r"^glm-\d+(\.\d+)?(-\w+)?$"),
                            re.compile(r"^(?P<series>glm)-\d+(?:\.\d+)?(?:-\w+)?$"), re.compile(r"^glm-(?P<version>\d+(?:\.\d+)?)(?:-\w+)?$")),
    # Official docs: Xiaomi MiMo Models API.
    "mimo": SourceDescriptor("mimo", "https://api.xiaomimimo.com", "/v1/models", "Authorization", "MIMO_API_KEY", "openai", re.compile(r"^mimo-v\d+\.\d+(-pro(-ultraspeed)?)?$"),
                             re.compile(r"^(?P<series>mimo)-v\d+\.\d+(?:-pro(?:-ultraspeed)?)?$"), re.compile(r"^mimo-v(?P<version>\d+\.\d+)(?:-pro(?:-ultraspeed)?)?$")),
    # Official docs: DashScope Model List API.
    "qwen": SourceDescriptor("qwen", "https://dashscope.aliyuncs.com", "/api/v1/models", "Authorization", "DASHSCOPE_API_KEY", "dashscope", re.compile(r"^qwen3"),
                             re.compile(r"^(?P<series>qwen)\d"), re.compile(r"^qwen(?P<version>\d+(?:\.\d+)?)")),
}
# fmt: on


@dataclass
class Comparison:
    candidates: list[str]
    suppressed: list[str]
    other_ids: list[str]
    series_models: dict[str, list[str]]


@dataclass
class ProviderReport:
    candidates: list[str]
    actionable_candidates: list[str]
    suppressed: list[str]
    other_ids: list[str]
    series_models: dict[str, list[str]]
    error: str | None
    status_changed: bool


def _environment_value(alias: str) -> str | None:
    if alias in os.environ:
        return os.environ[alias]
    return None


def _response_object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{name} response must be a JSON object")
    return value


def _is_fake_ip(address: str) -> bool:
    try:
        return ipaddress.ip_address(address) in _FAKE_IP_NETWORK
    except ValueError:
        return False


class _PinnedIPHTTPSConnection(HTTPSConnection):
    """Keep the origin host independent from urllib3's DNS target."""

    _origin_host: str

    @property
    def host(self) -> str:
        return self._origin_host.rstrip(".")

    @host.setter
    def host(self, value: str) -> None:
        self._origin_host = value
        self._dns_host = value


class _PinnedIPConnectionPool(HTTPSConnectionPool):
    """Create connections that dial a pinned IP while retaining the origin host."""

    ConnectionCls = cast(Any, _PinnedIPHTTPSConnection)
    pinned_ip = ""

    def _new_conn(self) -> Any:
        connection = cast(_PinnedIPHTTPSConnection, super()._new_conn())
        connection._dns_host = self.pinned_ip
        return connection


class PinnedIPHTTPSAdapter(HTTPAdapter):
    """Dial the pinned IP while SNI, Host, and certificate checks use the real hostname."""

    def __init__(self, pinned_ip: str, *args: Any, **kwargs: Any) -> None:
        self._pinned_ip = pinned_ip
        super().__init__(*args, **kwargs)

    @override
    def init_poolmanager(
        self,
        connections: int,
        maxsize: int,
        block: bool = False,
        **pool_kwargs: Any,
    ) -> None:
        self.poolmanager = PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            **pool_kwargs,
        )
        pinned_pool = type(
            "_PinnedIPConnectionPool",
            (_PinnedIPConnectionPool,),
            {"pinned_ip": self._pinned_ip},
        )
        self.poolmanager.pool_classes_by_scheme = {
            "http": HTTPConnectionPool,
            "https": pinned_pool,
        }


def _first_real_doh_address(value: object, endpoint: str, host: str) -> str:
    payload = _response_object(value, endpoint)
    answers = payload["Answer"]
    if not isinstance(answers, list):
        raise TypeError(f"{endpoint} Answer must be a list")
    for answer in answers:
        if not isinstance(answer, dict):
            raise TypeError(f"{endpoint} Answer entries must be objects")
        address = answer.get("data")
        if answer.get("type") in (1, "1") and isinstance(address, str) and not _is_fake_ip(address):
            return address
    raise ValueError(f"{endpoint} returned no usable A answer for {host}")


def _doh_real_ip(host: str) -> str:
    if host in _doh_cache:
        return _doh_cache[host]
    last_error: Exception | None = None
    for endpoint in _DOH_ENDPOINTS:
        try:
            response = requests.get(
                endpoint,
                headers={"accept": "application/dns-json"},
                params={"name": host, "type": "A"},
                timeout=_DOH_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            address = _first_real_doh_address(response.json(), endpoint, host)
            _doh_cache[host] = address
            return address
        except Exception as exc:
            last_error = exc
    raise ConnectionError(f"DoH resolution failed for {host}") from last_error


def _session_for_host(host: str) -> requests.Session | None:
    try:
        address_rows = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    ipv4_addresses: list[str] = []
    for family, _socket_type, _protocol, _canonical_name, socket_address in address_rows:
        address = socket_address[0]
        if family == socket.AF_INET and isinstance(address, str):
            ipv4_addresses.append(address)
    if not any(_is_fake_ip(address) for address in ipv4_addresses):
        return None
    # A detected fake IP must not fall back to the hijacked resolver if DoH fails.
    pinned_ip = _doh_real_ip(host)
    session = requests.Session()
    session.mount(f"https://{host}/", PinnedIPHTTPSAdapter(pinned_ip))
    return session


def _id_rows(value: object, field: str, source: str) -> list[str]:
    if not isinstance(value, list):
        raise TypeError(f"{source} {field} must be a list")
    ids: list[str] = []
    for row in value:
        if not isinstance(row, dict) or field not in row or not isinstance(row[field], str):
            raise ValueError(f"{source} {field} entries must carry a string id")
        model_id = row[field]
        if not model_id:
            raise ValueError(f"{source} {field} entries must not be empty")
        ids.append(model_id)
    if len(ids) != len(set(ids)):
        raise ValueError(f"{source} response contains duplicate model ids")
    return ids


def fetch_json(
    url: str, *, headers: dict[str, str], params: dict[str, str | int]
) -> dict[str, Any]:
    host = urlparse(url).hostname or ""
    session = _session_for_host(host)
    requester = session.get if session is not None else requests.get
    response = requester(
        url,
        headers={"User-Agent": _USER_AGENT, **headers},
        params=params,
        timeout=_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return _response_object(response.json(), url)


def _headers(source: SourceDescriptor, api_key: str) -> dict[str, str]:
    headers = dict(source.headers)
    if source.auth_header is None:
        return headers
    headers[source.auth_header] = (
        api_key if source.auth_header == "x-api-key" else f"Bearer {api_key}"
    )
    return headers


def _openai_models(source: SourceDescriptor, api_key: str) -> list[str]:
    payload = fetch_json(
        source.base_url + source.models_path,
        headers=_headers(source, api_key),
        params={},
    )
    if "data" not in payload:
        raise ValueError(f"{source.provider} response is missing data")
    return _id_rows(payload["data"], "id", source.provider)


def _anthropic_models(source: SourceDescriptor, api_key: str) -> list[str]:
    ids: list[str] = []
    after_id: str | None = None
    while True:
        params: dict[str, str | int] = {}
        if after_id is not None:
            params["after_id"] = after_id
        payload = fetch_json(
            source.base_url + source.models_path,
            headers=_headers(source, api_key),
            params=params,
        )
        if "data" not in payload or "has_more" not in payload:
            raise ValueError("claude response is missing data or has_more")
        page_ids = _id_rows(payload["data"], "id", "claude")
        if not isinstance(payload["has_more"], bool):
            raise TypeError("claude has_more must be boolean")
        ids.extend(page_ids)
        if not payload["has_more"]:
            break
        if not page_ids:
            raise ValueError("claude has_more requires a non-empty page")
        after_id = page_ids[-1]
    if len(ids) != len(set(ids)):
        raise ValueError("claude pages contain duplicate model ids")
    return ids


def _gemini_models(source: SourceDescriptor, api_key: str) -> list[str]:
    ids: list[str] = []
    params: dict[str, str | int] = {"key": api_key}
    while True:
        payload = fetch_json(
            source.base_url + source.models_path,
            headers=_headers(source, api_key),
            params=params,
        )
        if "models" not in payload:
            raise ValueError("gemini response is missing models")
        names = _id_rows(payload["models"], "name", "gemini")
        for name in names:
            if not name.startswith("models/"):
                raise ValueError(f"gemini model name lacks models/ prefix: {name!r}")
            ids.append(name.removeprefix("models/"))
        if "nextPageToken" not in payload:
            break
        token = payload["nextPageToken"]
        if not isinstance(token, str) or not token:
            raise ValueError("gemini nextPageToken must be a non-empty string")
        params = {"key": api_key, "pageToken": token}
    if len(ids) != len(set(ids)):
        raise ValueError("gemini pages contain duplicate model ids")
    return ids


def _dashscope_models(source: SourceDescriptor, api_key: str) -> list[str]:
    ids: list[str] = []
    total: int | None = None
    page_no = 1
    while True:
        payload = fetch_json(
            source.base_url + source.models_path,
            headers=_headers(source, api_key),
            params={"page_no": page_no, "page_size": _QWEN_PAGE_SIZE},
        )
        if "output" not in payload:
            raise ValueError("qwen response is missing output")
        output = _response_object(payload["output"], "qwen output")
        if "models" not in output or "total" not in output:
            raise ValueError("qwen output is missing models or total")
        page_ids = _id_rows(output["models"], "model", "qwen")
        page_total = output["total"]
        if not isinstance(page_total, int) or isinstance(page_total, bool) or page_total < 0:
            raise ValueError("qwen total must be a non-negative integer")
        if total is None:
            total = page_total
        elif page_total != total:
            raise ValueError("qwen page totals disagree")
        ids.extend(page_ids)
        if page_no * _QWEN_PAGE_SIZE >= total:
            break
        page_no += 1
    if len(ids) != total:
        raise ValueError("qwen total does not match the collected model ids")
    if len(ids) != len(set(ids)):
        raise ValueError("qwen pages contain duplicate model ids")
    return ids


def fetch_provider_models(source: SourceDescriptor, api_key: str) -> list[str]:
    if source.response_kind == "openai":
        return _openai_models(source, api_key)
    if source.response_kind == "anthropic":
        return _anthropic_models(source, api_key)
    if source.response_kind == "gemini":
        return _gemini_models(source, api_key)
    if source.response_kind == "dashscope":
        return _dashscope_models(source, api_key)
    raise RuntimeError(f"unknown response kind {source.response_kind!r}")


def _identity(source: SourceDescriptor, model_id: str) -> tuple[str, tuple[int, ...]] | None:
    series_match = source.series_pattern.match(model_id)
    version_match = source.version_pattern.match(model_id)
    if series_match is None or version_match is None:
        return None
    series = series_match.group("series") or source.provider
    groups = series_match.groupdict()
    if "preview" in groups and groups["preview"] is not None:
        series += groups["preview"]
    version = version_match.group("version")
    try:
        return series, tuple(int(part) for part in re.split(r"[.-]", version))
    except ValueError:
        return None


def compare_models(
    source: SourceDescriptor,
    upstream_ids: list[str],
    registry: Mapping[str, Any] = MODELS,
) -> Comparison:
    registry_ids = [
        model_id for model_id, spec in registry.items() if spec.provider == source.provider
    ]
    registry_series: dict[str, list[tuple[str, tuple[int, ...]]]] = {}
    for model_id in registry_ids:
        identity = _identity(source, model_id)
        if identity is not None:
            registry_series.setdefault(identity[0], []).append((model_id, identity[1]))
    candidates: list[str] = []
    suppressed: list[str] = []
    other_ids: list[str] = []
    series_models: dict[str, list[str]] = {}
    for model_id in upstream_ids:
        if source.family_pattern.match(model_id) is None:
            other_ids.append(model_id)
            continue
        if model_id in registry:
            continue
        identity = _identity(source, model_id)
        if identity is None:
            candidates.append(model_id)
            series_models[model_id] = []
            continue
        series, version = identity
        same_series = registry_series.get(series, [])
        series_models[model_id] = [known_id for known_id, _ in same_series]
        snapshot_suffix = _DATED_SNAPSHOT_SUFFIX.search(model_id)
        if snapshot_suffix is not None and model_id[: snapshot_suffix.start()] in registry:
            suppressed.append(model_id)
            continue
        if any(known_version > version for _, known_version in same_series):
            suppressed.append(model_id)
        else:
            candidates.append(model_id)
    return Comparison(candidates, suppressed, other_ids, series_models)


def _read_env_file(path: Path | None) -> dict[str, str]:
    if path is None:
        return read_env_aliases()
    return {key: value for key, value in dotenv_values(path).items() if value is not None}


def _api_key(source: SourceDescriptor, file_aliases: Mapping[str, str]) -> str | None:
    value = _environment_value(source.key_alias)
    if value is not None and value.strip():
        return value
    if source.key_alias in file_aliases and file_aliases[source.key_alias].strip():
        return file_aliases[source.key_alias]
    return None


def _state_path(state_dir: Path | None) -> Path:
    return (state_dir if state_dir is not None else ava_home() / "model-tracker") / "state.json"


def _load_state(path: Path) -> dict[str, dict[str, dict[str, object]]]:
    if not path.exists():
        return {"providers": {}}
    raw = _response_object(json.loads(path.read_text()), "model tracker state")
    if "providers" not in raw or not isinstance(raw["providers"], dict):
        raise ValueError("model tracker state is missing providers")
    providers: dict[str, dict[str, object]] = {}
    for provider, entry in raw["providers"].items():
        if not isinstance(provider, str) or not isinstance(entry, dict):
            raise TypeError("model tracker state has an invalid provider entry")
        if "reported" not in entry or "status" not in entry:
            raise ValueError("model tracker state provider is missing reported or status")
        if not isinstance(entry["reported"], list) or not all(
            isinstance(model_id, str) for model_id in entry["reported"]
        ):
            raise ValueError("model tracker state reported must be a list of strings")
        if not isinstance(entry["status"], str):
            raise TypeError("model tracker state status must be a string")
        status = entry["status"]
        if status != "ok" and (
            not status.startswith("error:") or not status.removeprefix("error:").strip()
        ):
            raise ValueError("model tracker state status must be ok or error:<message>")
        providers[provider] = {"reported": entry["reported"], "status": entry["status"]}
    return {"providers": providers}


def _save_state(path: Path, state: dict[str, dict[str, dict[str, object]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _status_changed(previous: object, current: str) -> bool:
    return isinstance(previous, str) and (previous == "ok") != (current == "ok")


def check_sources(
    file_aliases: Mapping[str, str], state: dict[str, dict[str, dict[str, object]]]
) -> dict[str, ProviderReport]:
    reports: dict[str, ProviderReport] = {}
    providers = state["providers"]
    for provider, source in SOURCES.items():
        if provider not in providers:
            entry: dict[str, object] = {"reported": []}
            previous_status: object = None
        else:
            entry = providers[provider]
            previous_status = entry["status"]
        api_key = _api_key(source, file_aliases)
        if api_key is None:
            status = f"error: missing {source.key_alias}"
            entry["status"] = status
            providers[provider] = entry
            reports[provider] = ProviderReport(
                [], [], [], [], {}, status, _status_changed(previous_status, status)
            )
            continue
        try:
            comparison = compare_models(source, fetch_provider_models(source, api_key))
        except Exception as exc:
            status = f"error: {str(exc) or type(exc).__name__}"
            entry["status"] = status
            providers[provider] = entry
            reports[provider] = ProviderReport(
                [], [], [], [], {}, status, _status_changed(previous_status, status)
            )
            continue
        raw = entry["reported"]
        if not isinstance(raw, list):
            raise TypeError("model tracker state reported must be a list")
        reported = set(raw)
        actionable = [model_id for model_id in comparison.candidates if model_id not in reported]
        entry["reported"] = sorted(reported | set(actionable) | set(comparison.suppressed))
        entry["status"] = "ok"
        providers[provider] = entry
        reports[provider] = ProviderReport(
            comparison.candidates,
            actionable,
            comparison.suppressed,
            comparison.other_ids,
            comparison.series_models,
            None,
            _status_changed(previous_status, "ok"),
        )
    return reports


def _report_payload(reports: Mapping[str, ProviderReport]) -> dict[str, object]:
    return {
        "providers": {provider: asdict(report) for provider, report in reports.items()},
        "actionable_candidates": {
            provider: report.actionable_candidates
            for provider, report in reports.items()
            if report.actionable_candidates
        },
        "status_changes": [
            provider for provider, report in reports.items() if report.status_changed
        ],
    }


def _markdown_report(reports: Mapping[str, ProviderReport]) -> str:
    lines = ["# Model update check", "", "## Actionable candidates"]
    actionable = False
    for provider, report in reports.items():
        if report.actionable_candidates:
            actionable = True
            lines.append(f"- **{provider}**: {', '.join(report.actionable_candidates)}")
    if not actionable:
        lines.append("- None")
    errors = [(provider, report.error) for provider, report in reports.items() if report.error]
    if errors:
        lines.extend(["", "## Provider errors"])
        lines.extend(f"- **{provider}**: {error}" for provider, error in errors)
    skipped = [
        (provider, report.suppressed) for provider, report in reports.items() if report.suppressed
    ]
    if skipped:
        lines.extend(["", "## Skipped older models"])
        lines.extend(f"- **{provider}**: {', '.join(models)}" for provider, models in skipped)
    other = [
        (provider, report.other_ids) for provider, report in reports.items() if report.other_ids
    ]
    if other:
        lines.extend(["", "## Other provider ids"])
        lines.extend(f"- **{provider}**: {', '.join(models)}" for provider, models in other)
    changed = [provider for provider, report in reports.items() if report.status_changed]
    if changed:
        lines.extend(["", "## Provider status changes", f"- {', '.join(changed)}"])
    return "\n".join(lines) + "\n"


def _write_report(directory: Path, markdown: str, payload: dict[str, object]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "last-report.md").write_text(markdown)
    (directory / "last-report.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check official provider model lists for Ava updates."
    )
    parser.add_argument("--check", action="store_true", help="check only (the default)")
    parser.add_argument("--env-file", type=Path, help="read provider keys from this dotenv file")
    parser.add_argument("--state-dir", type=Path, help="override the model-tracker state directory")
    parser.add_argument(
        "--write-report", type=Path, help="write last-report.md and last-report.json here"
    )
    args = parser.parse_args(argv)
    state_path = _state_path(args.state_dir)
    try:
        state = _load_state(state_path)
        reports = check_sources(_read_env_file(args.env_file), state)
        _save_state(state_path, state)
    except Exception as exc:
        print(f"model update tracker failed before provider reporting: {exc}")
        return 1
    payload = _report_payload(reports)
    markdown = _markdown_report(reports)
    print(markdown, end="")
    if args.write_report is not None:
        _write_report(args.write_report, markdown, payload)
    has_actionable = any(report.actionable_candidates for report in reports.values())
    has_status_change = any(report.status_changed for report in reports.values())
    has_error = any(report.error is not None for report in reports.values())
    if has_actionable or has_status_change:
        return 2
    return 1 if has_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
