"""Per-agent tab groups: the CDP client, the extension files and the failure policy.

Grouping is presentation only. A failure to group must leave the page open and
usable and leave a WARNING behind -- never an exception into the browser
operation, never silence (see
docs/decisions/engineering/design/contracts/2026-10-12-presentation-only-failure-degrades-with-warning.md).
"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from services.desktop.browser.tab_groups import grouping
from services.desktop.browser.tab_groups.grouping import (
    EXTENSION_DIR,
    AgentTabGroups,
    group_title,
)

EXTENSION_ID = "ext0123456789abcdefghijklmnopabcd"
PORT = 19477


class FakeCdpWS:
    """A browser-level CDP websocket: answers each request from a method table.

    ``handlers`` maps a CDP method to its ``result`` dict or a callable taking
    the request params. A CDP event is interleaved before every reply: the
    client must skip traffic that is not the reply to its own request.
    """

    def __init__(self, handlers: dict[str, Any]) -> None:
        self.handlers = handlers
        self.sent: list[dict[str, Any]] = []
        self._pending: list[str] = []

    async def __aenter__(self) -> FakeCdpWS:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def send(self, data: str) -> None:
        request = json.loads(data)
        self.sent.append(request)
        handler = self.handlers[request["method"]]
        outcome = handler(request.get("params", {})) if callable(handler) else handler
        reply: dict[str, Any] = {"id": request["id"]}
        if isinstance(outcome, Exception):
            reply["error"] = {"code": -32000, "message": str(outcome)}
        else:
            reply["result"] = outcome
        self._pending += [json.dumps({"method": "Target.targetInfoChanged", "params": {}})]
        self._pending += [json.dumps(reply)]

    async def recv(self) -> str:
        return self._pending.pop(0)

    def methods(self) -> list[str]:
        return [r["method"] for r in self.sent]


def _page(target_id: str, url: str = "https://example.com/") -> dict[str, str]:
    return {"id": target_id, "type": "page", "url": url}


SHIPPED_WORKER = next(EXTENSION_DIR.glob("group-*.js")).name


class ChromeModel:
    """The extension-relevant state of one Chrome, as CDP handlers.

    ``loaded``: the extension is loaded in this Chrome session (False on a fresh
    or restarted Chrome); ``worker``: the file name of its running service worker
    or None when it idled out. ``loadUnpacked`` loads the extension and starts the
    shipped worker, like Chrome does.
    """

    def __init__(
        self, *, loaded: bool = False, worker: str | None = None, script_delay: int = 0
    ) -> None:
        self.loaded = loaded
        self.worker = worker
        self.script_delay = script_delay  # probes answering "undefined" before the script ran

    def handlers(self) -> dict[str, Any]:
        return {
            "Extensions.getExtensions": self._get_extensions,
            "Extensions.loadUnpacked": self._load_unpacked,
            "Target.getTargets": self._get_targets,
            "Target.attachToTarget": {"sessionId": "S1"},
            "Runtime.evaluate": self._evaluate,
        }

    def _evaluate(self, params: dict[str, Any]) -> dict[str, Any]:
        if params["expression"] == "typeof avaGroup":
            if self.script_delay > 0:
                self.script_delay -= 1
                return {"result": {"type": "string", "value": "undefined"}}
            return {"result": {"type": "string", "value": "function"}}
        return {"result": {"type": "number", "value": 12}}

    def _get_extensions(self, _params: dict[str, Any]) -> dict[str, Any]:
        found = [{"id": EXTENSION_ID, "path": str(EXTENSION_DIR)}] if self.loaded else []
        return {"extensions": found}

    def _load_unpacked(self, params: dict[str, Any]) -> dict[str, Any]:
        assert params == {"path": str(EXTENSION_DIR)}
        self.loaded, self.worker = True, SHIPPED_WORKER
        return {"id": EXTENSION_ID}

    def _get_targets(self, _params: dict[str, Any]) -> dict[str, Any]:
        infos = [{"type": "page", "url": "https://example.com/", "targetId": "P1"}]
        if self.worker is not None:
            url = f"chrome-extension://{EXTENSION_ID}/{self.worker}"
            infos.append({"type": "service_worker", "url": url, "targetId": "SW1"})
        return {"targetInfos": infos}


def _chrome_handlers() -> dict[str, Any]:
    return ChromeModel().handlers()


class FakeChrome:
    """HTTP + websocket surfaces of one Chrome, installed over ``grouping``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, handlers: dict[str, Any]) -> None:
        self.listings: list[Any] = []
        self.ws = FakeCdpWS(handlers)
        self.http_urls: list[str] = []
        monkeypatch.setattr(grouping, "_http_json", self._http_json)
        monkeypatch.setattr(grouping.websockets, "connect", self._connect)

    def _connect(self, url: str, **kwargs: Any) -> FakeCdpWS:
        assert url == "ws://browser"
        return self.ws

    async def _http_json(self, url: str) -> Any:
        self.http_urls.append(url)
        if url.endswith("/json/version"):
            return {"webSocketDebuggerUrl": "ws://browser"}
        outcome = self.listings.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


_TASK_GROUPS: list[asyncio.TaskGroup] = []


@pytest.fixture(autouse=True)
async def _daemon_task_group() -> AsyncIterator[None]:
    """The daemon's root TaskGroup, which owns the background grouping tasks."""
    async with asyncio.TaskGroup() as tg:
        _TASK_GROUPS.append(tg)
        yield
    _TASK_GROUPS.clear()


def _groups() -> AgentTabGroups:
    return AgentTabGroups(PORT, _TASK_GROUPS[0])


async def _group(groups: AgentTabGroups, agent_id: int, before: frozenset[str] | None) -> None:
    """`group_new_page`, then wait for the background grouping it started."""
    await groups.group_new_page(agent_id, before)
    await asyncio.gather(*groups._tasks)


def _warnings(records: list[dict[str, Any]]) -> list[str]:
    return [r["message"] for r in records if r["level"].name == "WARNING"]


# ── The group title ──────────────────────────────────────────────────────────


def test_group_title_identifies_the_agent_and_only_the_agent() -> None:
    assert group_title(405) == "Ava #405"
    assert len({group_title(i) for i in range(1, 50)}) == 49  # unique per agent id


# ── The extension that ships with the package ───────────────────────────────


def test_worker_filename_carries_its_content_hash() -> None:
    """Chrome caches an extension's service-worker script in the profile and keeps
    running the cached copy when only the content changes (measured on Chrome 153;
    a bumped manifest version does not help). Only a new FILE NAME reliably
    loads new code, so the name must follow the content."""
    workers = sorted(EXTENSION_DIR.glob("group-*.js"))
    assert len(workers) == 1
    digest = hashlib.sha256(workers[0].read_bytes()).hexdigest()[:12]
    assert workers[0].name == f"group-{digest}.js", (
        f"worker content changed: rename it to group-{digest}.js and update "
        "extension/manifest.json background.service_worker"
    )


def test_manifest_points_at_the_worker_and_asks_only_for_what_it_uses() -> None:
    manifest = json.loads((EXTENSION_DIR / "manifest.json").read_text())
    worker = next(EXTENSION_DIR.glob("group-*.js"))
    assert manifest["manifest_version"] == 3
    assert manifest["background"] == {"service_worker": worker.name}
    assert sorted(manifest["permissions"]) == ["debugger", "tabGroups", "tabs"]
    assert sorted(p.name for p in EXTENSION_DIR.iterdir()) == sorted(["manifest.json", worker.name])


def test_worker_is_stateless_and_neither_colors_nor_deletes() -> None:
    """The group is found by title (no groupId survives anywhere), Chrome picks the
    color, and Chrome deletes a group when its last tab closes -- the worker has
    no delete path of its own."""
    source = next(EXTENSION_DIR.glob("group-*.js")).read_text()
    assert "tabGroups.query({ title })" in source
    assert not re.search(r"\bcolor\s*:", source)
    assert not re.search(r"ungroup|tabs\.remove|tabGroups\.remove|storage\.", source)


# ── snapshot: which page targets exist right now ───────────────────────────


async def test_snapshot_lists_page_targets_only(monkeypatch: pytest.MonkeyPatch) -> None:
    chrome = FakeChrome(monkeypatch, {})
    chrome.listings.append(
        [
            _page("T1"),
            {"id": "W1", "type": "service_worker", "url": "chrome-extension://x/sw.js"},
            {"id": "X1", "type": "other", "url": "devtools://devtools/"},
            _page("T2"),
        ]
    )
    assert await _groups().snapshot() == frozenset({"T1", "T2"})
    assert chrome.http_urls == [f"http://127.0.0.1:{PORT}/json/list"]


async def test_snapshot_failure_warns_and_returns_none(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    chrome = FakeChrome(monkeypatch, {})
    chrome.listings.append(OSError("connection refused"))
    assert await _groups().snapshot() is None
    messages = _warnings(loguru_records)
    assert len(messages) == 1 and "tab group" in messages[0]


# ── group_new_page: the CDP conversation ─────────────────────────────────────


async def test_group_new_page_files_the_new_target_into_the_agent_group(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    chrome = FakeChrome(monkeypatch, _chrome_handlers())
    chrome.listings.append([_page("T1"), _page("T2")])

    await _group(_groups(), 7, frozenset({"T1"}))

    ws = chrome.ws
    assert ws.methods() == [
        "Extensions.getExtensions",
        "Extensions.loadUnpacked",
        "Target.getTargets",
        "Target.attachToTarget",
        "Runtime.evaluate",  # waits for the worker script to define avaGroup
        "Runtime.evaluate",
    ]
    assert ws.sent[1]["params"] == {"path": str(EXTENSION_DIR)}
    assert ws.sent[3]["params"] == {"targetId": "SW1", "flatten": True}
    evaluate = ws.sent[5]
    assert evaluate["sessionId"] == "S1"
    assert evaluate["params"] == {
        "expression": 'avaGroup("T2", "Ava #7")',
        "awaitPromise": True,
        "returnByValue": True,
    }
    assert _warnings(loguru_records) == []


async def test_waits_for_the_worker_script_to_run_after_a_fresh_load(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    """On first load the worker target can be attachable before its script ran
    (measured: `avaGroup is not defined`), so the call waits for it."""
    model = ChromeModel(script_delay=3)
    chrome = FakeChrome(monkeypatch, model.handlers())
    chrome.listings.append([_page("T1"), _page("T2")])
    await _group(_groups(), 7, frozenset({"T1"}))
    evaluations = [
        r["params"]["expression"] for r in chrome.ws.sent if r["method"] == "Runtime.evaluate"
    ]
    assert evaluations == ["typeof avaGroup"] * 4 + ['avaGroup("T2", "Ava #7")']
    assert _warnings(loguru_records) == []


async def test_a_worker_that_never_defines_the_function_warns(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(grouping, "_WORKER_WAIT_S", 0.0)
    chrome = FakeChrome(monkeypatch, ChromeModel(script_delay=10**6).handlers())
    chrome.listings.append([_page("T1"), _page("T2")])
    await _group(_groups(), 7, frozenset({"T1"}))
    messages = _warnings(loguru_records)
    assert len(messages) == 1 and "did not define avaGroup" in messages[0]


async def test_a_live_worker_is_used_without_reloading_the_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reloading tears the running worker down; a call that attached to the dying
    target never gets its reply (measured). A live worker is therefore left alone."""
    model = ChromeModel(loaded=True, worker=SHIPPED_WORKER)
    chrome = FakeChrome(monkeypatch, model.handlers())
    chrome.listings.append([_page("T1"), _page("T2")])
    await _group(_groups(), 7, frozenset({"T1"}))
    assert "Extensions.loadUnpacked" not in chrome.ws.methods()
    assert chrome.ws.methods()[-1] == "Runtime.evaluate"


@pytest.mark.parametrize(
    ("model", "why"),
    [
        pytest.param(ChromeModel(), "fresh or restarted Chrome: not loaded", id="not-loaded"),
        pytest.param(
            ChromeModel(loaded=True, worker=None),
            "the worker idled out (~30s after its last event)",
            id="worker-idle",
        ),
        pytest.param(
            ChromeModel(loaded=True, worker="group-0000000000aa.js"),
            "an older worker file still runs (Chrome caches the worker script)",
            id="stale-worker-file",
        ),
    ],
)
async def test_extension_is_loaded_again_when_it_is_not_serving(
    model: ChromeModel, why: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    chrome = FakeChrome(monkeypatch, model.handlers())
    chrome.listings.append([_page("T1"), _page("T2")])
    await _group(_groups(), 7, frozenset({"T1"}))
    assert chrome.ws.methods().count("Extensions.loadUnpacked") == 1, why
    assert chrome.ws.methods()[-1] == "Runtime.evaluate"
    assert model.worker == SHIPPED_WORKER


async def test_reconnect_needs_no_memory_of_an_earlier_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two AgentTabGroups instances (a daemon restart) against one Chrome: the
    second finds the extension loaded and does not load it again; against a
    restarted Chrome it loads it."""
    model = ChromeModel()
    chrome = FakeChrome(monkeypatch, model.handlers())
    for _ in range(2):
        chrome.listings.append([_page("T1"), _page("T2")])
        await _group(_groups(), 7, frozenset({"T1"}))
    assert chrome.ws.methods().count("Extensions.loadUnpacked") == 1
    model.loaded, model.worker = False, None  # Chrome restarted
    chrome.listings.append([_page("T1"), _page("T2")])
    await _group(_groups(), 7, frozenset({"T1"}))
    assert chrome.ws.methods().count("Extensions.loadUnpacked") == 2


async def test_a_copy_of_the_extension_at_another_path_is_not_ours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = ChromeModel(loaded=True, worker=SHIPPED_WORKER)
    handlers = model.handlers()
    handlers["Extensions.getExtensions"] = {
        "extensions": [{"id": EXTENSION_ID, "path": "/somewhere/else/extension"}]
    }
    chrome = FakeChrome(monkeypatch, handlers)
    chrome.listings.append([_page("T1"), _page("T2")])
    await _group(_groups(), 7, frozenset({"T1"}))
    assert chrome.ws.methods().count("Extensions.loadUnpacked") == 1


async def test_group_new_page_skips_when_the_snapshot_failed(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    """A failed snapshot was already reported; nothing to diff against."""
    chrome = FakeChrome(monkeypatch, _chrome_handlers())
    await _group(_groups(), 7, None)
    assert chrome.ws.sent == [] and chrome.http_urls == []
    assert _warnings(loguru_records) == []


# ── Failure policy: warn, never raise, never guess ───────────────────────────


@pytest.mark.parametrize(
    ("after", "expected"),
    [
        pytest.param([_page("T1")], "0 new page targets", id="no-new-target"),
        pytest.param([_page("T1"), _page("T2"), _page("T3")], "2 new page targets", id="two"),
    ],
)
async def test_ambiguous_diff_warns_and_groups_nothing(
    after: list[dict[str, str]],
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
) -> None:
    chrome = FakeChrome(monkeypatch, _chrome_handlers())
    chrome.listings.append(after)
    await _group(_groups(), 7, frozenset({"T1"}))
    assert chrome.ws.sent == []  # never guesses which tab is the agent's
    messages = _warnings(loguru_records)
    assert len(messages) == 1
    assert expected in messages[0] and "agent 7" in messages[0]


def _evaluate_throws(params: dict[str, Any]) -> dict[str, Any]:
    if params["expression"] == "typeof avaGroup":
        return {"result": {"type": "string", "value": "function"}}
    return {
        "result": {"type": "object"},
        "exceptionDetails": {"exception": {"description": "Error: no tab for target T2"}},
    }


@pytest.mark.parametrize(
    ("override", "needle"),
    [
        pytest.param(
            {"Extensions.loadUnpacked": RuntimeError("load refused")},
            "load refused",
            id="extension-load-fails",
        ),
        pytest.param(
            {"Target.getTargets": {"targetInfos": []}},
            "service worker target not found",
            id="worker-target-missing",
        ),
        pytest.param(
            {"Runtime.evaluate": _evaluate_throws},
            "no tab for target T2",
            id="worker-throws",
        ),
        pytest.param(
            {"Target.attachToTarget": RuntimeError("attach denied")},
            "attach denied",
            id="attach-fails",
        ),
    ],
)
async def test_cdp_failure_warns_with_cause_and_never_raises(
    override: dict[str, Any],
    needle: str,
    monkeypatch: pytest.MonkeyPatch,
    loguru_records: list[dict[str, Any]],
) -> None:
    handlers = {**_chrome_handlers(), **override}
    monkeypatch.setattr(grouping, "_WORKER_WAIT_S", 0.0)
    chrome = FakeChrome(monkeypatch, handlers)
    chrome.listings.append([_page("T1"), _page("T2")])

    await _group(_groups(), 7, frozenset({"T1"}))  # must not raise

    messages = _warnings(loguru_records)
    assert len(messages) == 1
    assert needle in messages[0] and "Ava #7" in messages[0]
    assert "still open" in messages[0]  # tells the reader the page itself is fine


async def test_chrome_unreachable_warns_and_never_raises(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    chrome = FakeChrome(monkeypatch, _chrome_handlers())
    chrome.listings.append(OSError("connection refused"))
    await _group(_groups(), 7, frozenset({"T1"}))
    assert len(_warnings(loguru_records)) == 1


async def test_a_wedged_chrome_is_bounded(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    import asyncio

    async def hang(url: str) -> Any:
        await asyncio.sleep(3600)

    monkeypatch.setattr(grouping, "_http_json", hang)
    monkeypatch.setattr(grouping, "_LISTING_TIMEOUT_S", 0.05)
    assert await _groups().snapshot() is None
    await _group(_groups(), 7, frozenset())
    assert len(_warnings(loguru_records)) == 2


def test_extension_dir_ships_inside_the_package() -> None:
    """Hatch ships every file under the `services` package; the extension must
    live there so a non-editable install can still `loadUnpacked` it."""
    assert EXTENSION_DIR.is_dir()
    services_root = Path(grouping.__file__).resolve().parents[3]
    assert services_root.name == "services"
    assert EXTENSION_DIR.resolve().is_relative_to(services_root)


# ── Off the daemon's lock: the page creation never waits for the grouping ──────


class WedgedConnect:
    """`websockets.connect` to a Chrome that accepts the connection and never answers."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    def __call__(self, url: str, **kwargs: Any) -> WedgedConnect:
        return self

    async def __aenter__(self) -> WedgedConnect:
        self.entered.set()
        await asyncio.Event().wait()
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


async def test_group_new_page_returns_before_the_grouping_runs(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    """The caller holds the daemon's serial lock: it gets control back after the one
    target listing, and the grouping (load, attach, call) happens afterwards."""
    chrome = FakeChrome(monkeypatch, _chrome_handlers())
    chrome.listings.append([_page("T1"), _page("T2")])
    groups = _groups()

    await groups.group_new_page(7, frozenset({"T1"}))

    assert chrome.ws.sent == []  # nothing but the listing has happened yet
    assert len(groups._tasks) == 1
    await asyncio.gather(*groups._tasks)
    assert chrome.ws.methods()[-1] == "Runtime.evaluate"
    assert groups._tasks == set()
    assert _warnings(loguru_records) == []


async def test_a_wedged_chrome_costs_the_caller_nothing(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    chrome = FakeChrome(monkeypatch, _chrome_handlers())
    wedged = WedgedConnect()
    monkeypatch.setattr(grouping.websockets, "connect", wedged)
    chrome.listings.append([_page("T1"), _page("T2")])
    groups = _groups()

    await asyncio.wait_for(groups.group_new_page(7, frozenset({"T1"})), timeout=1.0)
    await asyncio.wait_for(
        wedged.entered.wait(), timeout=1.0
    )  # grouping is stuck in the background

    groups.cancel_pending()
    await asyncio.gather(*groups._tasks, return_exceptions=True)
    assert groups._tasks == set()
    assert (
        _warnings(loguru_records) == []
    )  # a cancelled grouping is the daemon's doing, not a failure


async def test_a_grouping_that_outlives_its_budget_warns(
    monkeypatch: pytest.MonkeyPatch, loguru_records: list[dict[str, Any]]
) -> None:
    chrome = FakeChrome(monkeypatch, _chrome_handlers())
    monkeypatch.setattr(grouping.websockets, "connect", WedgedConnect())
    monkeypatch.setattr(grouping, "_GROUP_TIMEOUT_S", 0.05)
    chrome.listings.append([_page("T1"), _page("T2")])
    await _group(_groups(), 7, frozenset({"T1"}))
    assert len(_warnings(loguru_records)) == 1


async def test_cancelling_and_failing_groupings_leave_no_unretrieved_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        broken = {**_chrome_handlers(), "Extensions.getExtensions": RuntimeError("boom")}
        chrome = FakeChrome(monkeypatch, broken)
        chrome.listings.append([_page("T1"), _page("T2")])
        failing = _groups()
        await _group(failing, 7, frozenset({"T1"}))  # fails inside the task, reported by it

        monkeypatch.setattr(grouping.websockets, "connect", WedgedConnect())
        chrome.listings.append([_page("T1"), _page("T2")])
        cancelled = _groups()
        await cancelled.group_new_page(8, frozenset({"T1"}))
        cancelled.cancel_pending()
        await asyncio.gather(*cancelled._tasks, return_exceptions=True)

        del failing, cancelled
        gc.collect()  # "Task exception was never retrieved" is reported when a task is collected
        await asyncio.sleep(0)
        assert unhandled == []
    finally:
        loop.set_exception_handler(None)


class RacyWorkerWS(FakeCdpWS):
    """A worker whose `avaGroup` is not atomic, like the real one: it looks the group
    up by title, and creates it only after an await. Two groupings running at the
    same time both find nothing and both create."""

    def __init__(self, handlers: dict[str, Any], groups: list[str]) -> None:
        super().__init__(handlers)
        self.groups = groups
        self._exists: bool | None = None
        self._title = ""

    async def send(self, data: str) -> None:
        await super().send(data)
        request = json.loads(data)
        match = re.fullmatch(
            r'avaGroup\("[^"]+", "([^"]+)"\)', request["params"].get("expression", "")
        )
        if request["method"] == "Runtime.evaluate" and match:
            self._title = match.group(1)
            self._exists = self._title in self.groups  # the lookup happens now ...

    async def recv(self) -> str:
        if self._exists is not None:
            await asyncio.sleep(0.02)  # ... the creation after an await
            if not self._exists:
                self.groups.append(self._title)
            self._exists = None
        return await super().recv()


async def test_back_to_back_pages_of_one_agent_make_one_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two groupings of one agent in flight at once would each find no group by title
    and each create it. The groupings run one at a time (their own lock)."""
    created: list[str] = []
    model = ChromeModel(loaded=True, worker=SHIPPED_WORKER)
    listings: list[list[dict[str, str]]] = [
        [_page("T1"), _page("T2")],
        [_page("T1"), _page("T2"), _page("T3")],
    ]

    async def http_json(url: str) -> Any:
        if url.endswith("/json/version"):
            return {"webSocketDebuggerUrl": "ws://browser"}
        return listings.pop(0)

    def connect(url: str, **kwargs: Any) -> RacyWorkerWS:
        return RacyWorkerWS(model.handlers(), created)

    monkeypatch.setattr(grouping, "_http_json", http_json)
    monkeypatch.setattr(grouping.websockets, "connect", connect)
    groups = _groups()

    await groups.group_new_page(7, frozenset({"T1"}))
    await groups.group_new_page(7, frozenset({"T1", "T2"}))
    assert len(groups._tasks) == 2  # both in flight together
    await asyncio.gather(*groups._tasks)

    assert created == ["Ava #7"]
