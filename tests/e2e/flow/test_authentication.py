from __future__ import annotations

import httpx
import pytest

from base.host.net.auth import bearer_header
from tests.e2e._credentials import current_credentials
from tests.e2e._ports import GATEWAY_URL


@pytest.mark.scenario("tests.e2e.fakes.scenarios.sdk_effects:build_exec_failures")
def test_the_gateway_admits_only_valid_credentials(gateway_proc: str) -> None:
    credentials = current_credentials()
    wrong = httpx.get(f"{GATEWAY_URL}/api/agents", headers=bearer_header("not-a-credential"))
    assert wrong.status_code == 401
    for token in (credentials.secret, credentials.gateway_token, credentials.runner_token):
        admitted = httpx.get(f"{GATEWAY_URL}/api/agents", headers=bearer_header(token))
        assert admitted.status_code == 200, token
    assert httpx.get(f"{GATEWAY_URL}/api/agents").status_code == 200
