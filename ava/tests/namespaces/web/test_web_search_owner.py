from __future__ import annotations

import importlib
import json
import threading
from collections.abc import Iterator
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import SecretStr

import ava
from ava.sdk_surface import install, process_context
from ava.sdk_surface.installation import installed
from ava.web import wiring
from base.agents.context import AvaContext
from base.agents.context.clients import ClientSet
from base.agents.sdk.call_policy import SamplingPolicy, SamplingPolicyOwner
from base.config import ConfigBoot
from base.config.service_read import ConfigAuthority
from base.packages.plugins.extensions import EMPTY
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events


class _LoopbackWeb(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict[str, Any]]] = []

    def do_GET(self) -> None:
        _LoopbackWeb.requests.append(
            {"path": self.path, "token": self.headers.get("X-Subscription-Token")}
        )
        body = json.dumps(
            {
                "web": {
                    "results": [
                        {
                            "title": "owner title",
                            "url": "https://example.com/result",
                            "description": "owner snippet",
                        }
                    ]
                }
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def loopback_web() -> Iterator[str]:
    """One loopback HTTP endpoint that records every request it receives."""
    _LoopbackWeb.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackWeb)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def non_model_installation(
    loopback_web: str, tmp_path: Path, emitted_events: EmittedEvents
) -> Iterator[ConfigAuthority]:
    """Install the real SDK over an authority with no model catalog, restored after the test."""
    boot = ConfigBoot()
    boot.set_field("brave_api_key", SecretStr("owner-key"))
    boot.set_field("web_brave_search_endpoint", f"{loopback_web}/search")
    boot.set_field("web_jina_reader_base", f"{loopback_web}/reader/")
    authority = ConfigAuthority.deferred(
        runtime=boot.view,
        build_all_domains=boot.ensure_eager,
        env_path=tmp_path / "config.env",
    )
    sampling = SamplingPolicyOwner(reader=SamplingPolicy)
    previous = getattr(ava, "context", None)
    process_context.bind_context(
        replace(
            previous or AvaContext(), clients=ClientSet(pipeline_factory=emitted_events.producer)
        )
    )
    install.install(EMPTY, authority=authority, sampling=sampling, producer=emitted_events.producer)
    try:
        yield authority
    finally:
        install.uninstall()
        sampling.close()
        if previous is None:
            process_context.unbind_context()
        else:
            process_context.bind_context(previous)


def test_search_runs_without_a_model_catalog(
    non_model_installation: ConfigAuthority, emitted_events: EmittedEvents
) -> None:
    results = ava.web.search(["owner query"])

    assert results == [
        [
            ava.web.SearchResult(
                title="owner title",
                url="https://example.com/result",
                snippet="owner snippet",
                kind="web",
            )
        ]
    ]
    assert _LoopbackWeb.requests == [
        {"path": "/search?q=owner+query&count=10", "token": "owner-key"}
    ]
    assert [call["fn"] for call in emitted_events.attributes("sdk_call")] == ["web.search"]


def test_fetch_reports_a_missing_model_configuration(
    non_model_installation: ConfigAuthority,
) -> None:
    with pytest.raises(ava.web.FetchError) as caught:
        ava.web.fetch([("https://example.com/article", "summarize")])

    assert "model configuration" in str(caught.value)
    assert "ModelCatalog" in str(caught.value)
    assert _LoopbackWeb.requests == []


def test_search_reads_no_model_configuration(non_model_installation: ConfigAuthority) -> None:
    deps = wiring.search_deps()

    assert deps.brave.api_key is not None
    assert ava.web.search(["owner query"])[0][0].title == "owner title"


def test_wiring_import_reads_no_configuration() -> None:
    assert installed() is None
    importlib.reload(wiring)
