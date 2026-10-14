"""The e2e stack's credentials: a real cluster secret and a real write generation.

The cluster API always authenticates, so the direct-process stack authenticates the
way a started cluster does, with the same stores and none of the launcher's custody:

- the e2e home gets a cluster secret (`AVA_CLUSTER_SECRET`, declared in its `.env`
  like every cluster-scope key) and an active write generation in its private ledger
  (`base.host.db_authority.tests.generation.seed_active_generation`, the ledger `ava start`
  births at first start);
- the test process carries the generation marker (`AVA_DB_GENERATION`, the one a launcher
  delivers beside the login), which tells the boot pass of every process descending from
  it that its database login was delivered: this stack has no root custody to admit an
  operator delivery from the ledger;
- each process the stack starts is delivered the machine API token of its class in
  `AVA_API_TOKEN`, as `cli.commands.data_plane.bringup.api_delivery` delivers it:
  the gateway its gateway-class token (so its `/ops` dials are accepted), the ops
  daemon and the agent host the runner-class token (agent children inherit it);
- the test process reaches the gateway as an operator would, with the human secret as
  a Bearer header (`gateway_credentials` / `install_gateway_client_credentials`).

No product admission rule is bypassed or weakened for the fixture.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext

from base.config import settings
from base.host.db_authority.tests.generation import seed_active_generation
from base.host.net.auth import API_TOKEN_ENV, bearer_header
from tests.e2e._ports import GATEWAY_URL

__all__ = [
    "E2ECredentials",
    "current_credentials",
    "install_browser_login",
    "mint_credentials",
]


@dataclass(frozen=True)
class E2ECredentials:
    """The e2e home's human secret and its generation's machine API tokens."""

    secret: str
    gateway_token: str
    runner_token: str

    def service_env(self, cls: Literal["gateway", "runner"]) -> dict[str, str]:
        """The launch-environment delivery of one service class: its API token."""
        token = self.gateway_token if cls == "gateway" else self.runner_token
        return {API_TOKEN_ENV: token}


_current: E2ECredentials | None = None
_previous_secret = ""


def mint_credentials(home: Path) -> E2ECredentials:
    """Seed `home`'s ledger with an active generation and mint its human secret, which
    becomes this process's configured cluster secret until `forget_credentials`."""
    global _current, _previous_secret  # noqa: PLW0603 — package-scoped, set once by `e2e_process_env`
    tokens = seed_active_generation(home).api
    _previous_secret = settings.data_plane.cluster_secret
    _current = E2ECredentials(
        secret=secrets.token_urlsafe(32),
        gateway_token=tokens.gateway,
        runner_token=tokens.runner,
    )
    settings.data_plane.cluster_secret = _current.secret
    return _current


def current_credentials() -> E2ECredentials:
    if _current is None:
        raise RuntimeError("the e2e credentials are minted by the `e2e_process_env` fixture")
    return _current


def forget_credentials() -> None:
    global _current  # noqa: PLW0603
    settings.data_plane.cluster_secret = _previous_secret
    _current = None


@pytest.fixture(autouse=True)
def gateway_client_credentials(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give this test's own HTTP calls to the e2e gateway the human secret as bearer.

    Scoped to the gateway's origin and to requests carrying no `Authorization` of their
    own, so the test's calls to other servers and its explicit headers are untouched.
    """
    original = httpx.Client.send

    def send(client: httpx.Client, request: httpx.Request, **kwargs: object) -> httpx.Response:
        if str(request.url).startswith(GATEWAY_URL) and "authorization" not in request.headers:
            request.headers.update(bearer_header(current_credentials().secret))
        return original(client, request, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(httpx.Client, "send", send)
    yield


def _gateway_serving() -> bool:
    """True once the gateway answers HTTP. The retained listening socket accepts
    connections before any process serves them, so only a response proves it.

    A session that never minted credentials runs without the stack (the
    visual-baselines workflow renders its stubbed pages standalone), so there is
    nothing to log in to, and the probe must answer without a request: the
    autouse client-credential hook intercepts this probe's own call and would
    fail it for the missing mint.
    """
    if _current is None:
        return False
    try:
        return httpx.get(f"{GATEWAY_URL}/api/health", timeout=1.0).is_success
    except httpx.HTTPError:
        return False


def install_browser_login(browser: Browser) -> None:
    """Make every context this browser creates start logged in, as a user's browser is.

    The gateway sets its session cookie on the login response; it lives in the
    context's cookie jar and rides with the page's API calls. A context created while
    no gateway serves (a test that stubs the API and never starts one) has nothing to
    log in to and is left alone; once the gateway serves, a refused login fails loudly.
    """
    create = browser.new_context

    def new_context(*args: Any, **kwargs: Any) -> BrowserContext:
        context = create(*args, **kwargs)
        if _gateway_serving():
            login = context.request.post(
                f"{GATEWAY_URL}/api/auth/login", data={"password": current_credentials().secret}
            )
            assert login.ok, f"e2e browser login refused: {login.status}"
        return context

    browser.new_context = new_context  # pyright: ignore[reportAttributeAccessIssue]
