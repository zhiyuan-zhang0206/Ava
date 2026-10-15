"""One-time login tickets: the credential behind the link `ava start` prints.

A human-secret caller issues a ticket (`POST /api/auth/login-ticket`); a browser
redeems it, without any credential, for the same session cookie a password
login sets (`POST /api/auth/login-ticket/redeem`). The ticket is a short-lived
`web_sessions` row: single use, expiring, never a session cookie itself, and
bound to the credential that issued it.
"""

from __future__ import annotations

import httpx2
import psycopg
import pytest
from fastapi.testclient import TestClient

from base import config
from base.host.net.auth import bearer_header, cookie_name
from gateway.http.auth.session_store import LOGIN_TICKET_TTL_SECONDS
from gateway.tests.agents.gateway_auth import disable_default_gateway_credentials
from tests.fixtures.gateway_config import gateway_test_client

_SECRET = "test-cluster-secret"  # noqa: S105 — test fixture
_ISSUE = "/api/auth/login-ticket"
_REDEEM = "/api/auth/login-ticket/redeem"


@pytest.fixture(autouse=True)
def patch_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """A known secret and anonymous clients: each test says what credential it presents."""
    disable_default_gateway_credentials(monkeypatch)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)


def _issue(client: TestClient) -> str:
    response = client.post(_ISSUE, headers=bearer_header(_SECRET))
    assert response.status_code == 200, response.text
    return response.json()["ticket"]


def _redeem(client: TestClient, ticket: str) -> httpx2.Response:
    return client.post(_REDEEM, json={"ticket": ticket})


def _cookie_headers(value: str) -> dict[str, str]:
    return {"Cookie": f"{cookie_name()}={value}"}


def test_issuing_needs_a_credential() -> None:
    with gateway_test_client() as client:
        anonymous = client.post(_ISSUE)
        wrong = client.post(_ISSUE, headers=bearer_header("not-the-secret"))
    assert anonymous.status_code == 401
    assert wrong.status_code == 401


def test_a_ticket_is_short_lived_and_not_cacheable(db_conn: psycopg.Connection) -> None:
    with gateway_test_client() as client:
        response = client.post(_ISSUE, headers=bearer_header(_SECRET))
    assert response.status_code == 200
    body = response.json()
    assert body["expires_in_seconds"] == LOGIN_TICKET_TTL_SECONDS <= 120
    assert response.headers["cache-control"] == "no-store"
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT extract(epoch FROM expires_at - created_at) FROM web_sessions WHERE id = %s",
            (body["ticket"],),
        )
        row = cur.fetchone()
    assert row is not None
    assert 0 < float(row[0]) <= LOGIN_TICKET_TTL_SECONDS


def test_redeeming_sets_the_same_session_cookie_a_login_sets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.settings.gateway, "session_ttl_seconds", 456)
    with gateway_test_client() as client:
        ticket = _issue(client)
        redeemed = _redeem(client, ticket)
        logged_in = client.post("/api/auth/login", json={"password": _SECRET})

    assert redeemed.status_code == 200 and redeemed.json() == {"ok": True}
    set_cookie = redeemed.headers["Set-Cookie"]
    assert set_cookie.startswith(f"{cookie_name()}=")
    for flag in ("HttpOnly", "SameSite=Lax", "Path=/", "Max-Age=456"):
        assert flag in set_cookie
    # Same credential, same mint: the session id differs only after the mint.
    mint = redeemed.cookies[cookie_name()].partition(".")[0]
    assert mint == logged_in.cookies[cookie_name()].partition(".")[0]
    assert mint.startswith("human-")


def test_the_redeemed_cookie_authenticates_and_no_cookie_does_not() -> None:
    with gateway_test_client() as client:
        ticket = _issue(client)
        cookie = _redeem(client, ticket).cookies[cookie_name()]
        client.cookies.clear()

        without = client.get("/api/agents")
        with_cookie = client.get("/api/agents", headers=_cookie_headers(cookie))
        check = client.get("/api/auth/check", headers=_cookie_headers(cookie))

    assert without.status_code == 401
    assert with_cookie.status_code == 200
    assert check.json() == {"authenticated": True}


def test_a_ticket_opens_exactly_one_session() -> None:
    with gateway_test_client() as client:
        ticket = _issue(client)
        first = _redeem(client, ticket)
        client.cookies.clear()
        replay = _redeem(client, ticket)

    assert first.status_code == 200
    assert replay.status_code == 401
    assert replay.json()["code"] == "invalid_login_ticket"
    assert cookie_name() not in replay.cookies


def test_an_expired_ticket_is_refused(db_conn: psycopg.Connection) -> None:
    with gateway_test_client() as client:
        ticket = _issue(client)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE web_sessions SET expires_at = now() - interval '1 second' WHERE id = %s",
                (ticket,),
            )
        db_conn.commit()
        response = _redeem(client, ticket)

    assert response.status_code == 401
    assert response.json()["code"] == "invalid_login_ticket"
    assert cookie_name() not in response.cookies


def test_a_ticket_is_not_a_session_cookie() -> None:
    """The ticket row is a valid, live `web_sessions` id until it is spent, and
    presenting it as the session cookie must still authenticate nothing."""
    with gateway_test_client() as client:
        ticket = _issue(client)
        agents = client.get("/api/agents", headers=_cookie_headers(ticket))
        check = client.get("/api/auth/check", headers=_cookie_headers(ticket))
        listed = client.get("/api/auth/sessions", headers=bearer_header(_SECRET))
        spendable = _redeem(client, ticket)

    assert agents.status_code == 401
    assert check.json() == {"authenticated": False}
    assert ticket not in {row["id"] for row in listed.json()}
    assert spendable.status_code == 200  # presenting it as a cookie did not spend it


def test_a_session_cookie_is_not_a_ticket() -> None:
    with gateway_test_client() as client:
        cookie = client.post("/api/auth/login", json={"password": _SECRET}).cookies[cookie_name()]
        client.cookies.clear()
        response = _redeem(client, cookie)
        still_valid = client.get("/api/agents", headers=_cookie_headers(cookie))

    assert response.status_code == 401
    assert still_valid.status_code == 200


@pytest.mark.parametrize(
    "ticket",
    [
        "",
        "garbage",
        "ticket.",
        "ticket..",
        "ticket.human-abc.",
        "ticket.human-abc.unknown",
        "x.y.z",
    ],
)
def test_malformed_and_unknown_tickets_get_the_same_refusal(ticket: str) -> None:
    with gateway_test_client() as client:
        response = _redeem(client, ticket)
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_login_ticket"


def test_a_missing_ticket_field_is_a_refusal_not_a_validation_error() -> None:
    with gateway_test_client() as client:
        response = client.post(_REDEEM, json={})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_login_ticket"


def test_a_ticket_dies_with_the_credential_that_issued_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with gateway_test_client() as client:
        ticket = _issue(client)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", "rotated-" + "r" * 40)
    with gateway_test_client() as rotated_client:
        response = _redeem(rotated_client, ticket)
    monkeypatch.setattr(config.settings.data_plane, "cluster_secret", _SECRET)
    with gateway_test_client() as restored_client:
        after_restore = _redeem(restored_client, ticket)

    assert response.status_code == 401
    assert after_restore.status_code == 401  # spent by the refused attempt


def test_a_session_the_ticket_opened_can_issue_the_next_ticket() -> None:
    with gateway_test_client() as client:
        cookie = _redeem(client, _issue(client)).cookies[cookie_name()]
        client.cookies.clear()
        issued = client.post(_ISSUE, headers=_cookie_headers(cookie))
    assert issued.status_code == 200


def test_failed_redemptions_lock_the_ip_like_failed_logins() -> None:
    with gateway_test_client() as client:
        for _ in range(config.settings.gateway.login_max_failures):
            assert _redeem(client, "garbage").status_code == 401
        locked = _redeem(client, "garbage")
        genuine = _redeem(client, _issue(client))
    assert locked.status_code == 429
    assert locked.json()["code"] == "login_rate_limited"
    assert "retry-after" in locked.headers
    assert genuine.status_code == 429  # a valid ticket is not evaluated while locked
