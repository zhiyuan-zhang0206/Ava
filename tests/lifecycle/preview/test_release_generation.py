"""The preview's write-generation evidence: observation and the stale-writer verdict.

The probe itself runs against real PostgreSQL in
tests/lifecycle/db_authority/test_release_fence.py; here its verdict and the
observer's generation record, without a database.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts.preview import release_generation
from shared.cluster import authority

_HELD = {"event": "held", "xid": 812}
_ENDED = {"event": "held-ended", "error": "AdminShutdown: terminating connection"}
_REFUSED = {"event": "reconnect", "committed": False, "error": "OperationalError: denied"}


def test_the_probe_proves_the_fence_only_with_an_aborted_transaction_and_no_commit() -> None:
    verdict = release_generation.judge([_HELD, _ENDED, _REFUSED, _REFUSED], "aborted")
    assert verdict == {
        "held_ended": _ENDED["error"],
        "refused_reconnects": 2,
        "last_refusal": _REFUSED["error"],
        "held_transaction": "aborted",
    }


@pytest.mark.parametrize(
    ("events", "status", "reason"),
    [
        ([_HELD], "aborted", "never ended"),
        (
            [_HELD, _ENDED, {"event": "reconnect", "committed": True, "xid": 9}],
            "aborted",
            "committed",
        ),
        ([_HELD, _ENDED], "aborted", "never retried"),
        ([_HELD, _ENDED, _REFUSED], "committed", "not aborted"),
        ([_HELD, _ENDED, _REFUSED], None, "not aborted"),
    ],
)
def test_the_probe_verdict_refuses_any_missing_or_contradicting_evidence(
    events: list[dict[str, Any]], status: str | None, reason: str
) -> None:
    with pytest.raises(RuntimeError, match=reason):
        release_generation.judge(events, status)


def test_the_observer_records_the_served_generation_without_credentials(
    tmp_path: Path, seed_write_generation: Callable[[Path], Any]
) -> None:
    home = (tmp_path / "home").resolve()
    home.mkdir(mode=0o700)
    secret = seed_write_generation(home)
    authority.ensure_pooler_admin(
        home, encrypt=lambda name, _pw: f"SCRAM-SHA-256$4096:c2VlZA==${name}"
    )
    userlist = home / "pgbouncer" / "userlist.txt"
    userlist.parent.mkdir()
    userlist.write_bytes(authority.render_userlist(home, authority.active_generation(home)))
    observed = release_generation.observe(home)
    assert observed["number"] == 0 and observed["roles"] == ["ava_g0_gateway", "ava_g0_runner"]
    assert secret.roles.gateway.password not in repr(observed)
    userlist.write_bytes(userlist.read_bytes().splitlines(keepends=True)[0])
    with pytest.raises(RuntimeError, match="does not serve exactly"):
        release_generation.observe(home)


def test_evidence_labels_are_path_safe(tmp_path: Path) -> None:
    context = release_generation.Context(tmp_path)
    assert context.path("fence", "ab") == tmp_path / "fence-ab.json"
    with pytest.raises(ValueError, match="label"):
        context.path("generation", "../escape")
