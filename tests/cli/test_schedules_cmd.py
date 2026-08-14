"""`ava schedules` thin-client commands — each verb forwards to the right
/api/schedules route and renders the response, verified without a live gateway.

The cmd_* functions import `shared.http_dial` inside their bodies, so patching
the module attributes here takes effect at call time (same seam as
`tests/cli/test_agents_cmd.py`). What's under test is the client-side logic:
name-or-id resolution, the script-source XOR, the exclude-unset update body,
the tri-state enable/disable flag, and the error-detail surfacing.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import psycopg
import pytest

from cli.commands import schedules as _sched
from cli.main import _build_parser


class _FakeResp:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> object:
        return self._payload


def _row(
    schedule_id: int = 7,
    name: str = "nightly",
    *,
    enabled: bool = True,
    status: str = "running",
) -> dict[str, object]:
    """A full ScheduleView row — every field the renderer reads."""
    return {
        "id": schedule_id,
        "name": name,
        "description": "nightly digest",
        "command": "python schedule.py",
        "enabled": enabled,
        "status": status,
        "last_error": None,
        "created_at": "2026-07-01T00:00:00Z",
        "updated_at": "2026-07-01T00:00:00Z",
        "script": "print('hi')\n",
    }


@pytest.fixture(autouse=True)
def _gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    monkeypatch.setattr("shared.machine.gateway_auth_headers", dict)


def _patch(monkeypatch: pytest.MonkeyPatch, verb: str, payload: object, status: int = 200) -> dict:
    """Patch one shared.http_dial verb; record url/json/params, return `payload`."""
    from shared import http_dial

    seen: dict[str, object] = {}

    def fake(url: str, **kwargs: object) -> _FakeResp:
        seen["url"] = url
        seen["json"] = kwargs.get("json")
        seen["params"] = kwargs.get("params")
        return _FakeResp(payload, status)

    monkeypatch.setattr(http_dial, verb, fake)
    return seen


# ── ls / get ──


def test_ls_renders_rows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _patch(
        monkeypatch,
        "get",
        [_row(7, "nightly"), _row(12, "hourly", enabled=False, status="stopped")],
    )
    assert _sched.cmd_schedules_ls() == 0
    assert seen["url"] == "http://gw:8000/api/schedules"
    out = capsys.readouterr().out
    assert "nightly" in out and "running" in out
    assert "hourly" in out and "stopped" in out
    assert "no" in out  # the disabled row's enabled column


def test_ls_empty(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _patch(monkeypatch, "get", [])
    assert _sched.cmd_schedules_ls() == 0
    assert "(no schedules)" in capsys.readouterr().out


def test_get_by_numeric_id_skips_the_name_lookup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A numeric identifier addresses the row directly — no list round-trip."""
    seen = _patch(monkeypatch, "get", _row(7))
    assert _sched.cmd_schedules_get("7") == 0
    assert seen["url"] == "http://gw:8000/api/schedules/7"
    out = capsys.readouterr().out
    assert "print('hi')" in out  # `get` includes the script body


def test_get_by_name_resolves_via_list(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from shared import http_dial

    urls: list[str] = []

    def fake_get(url: str, **_kw: object) -> _FakeResp:
        urls.append(url)
        # First call is the name->id list lookup; second is the row fetch.
        return _FakeResp([_row(7, "nightly")] if url.endswith("/api/schedules") else _row(7))

    monkeypatch.setattr(http_dial, "get", fake_get)
    assert _sched.cmd_schedules_get("nightly") == 0
    assert urls == ["http://gw:8000/api/schedules", "http://gw:8000/api/schedules/7"]


def test_unknown_name_fails_without_a_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unresolvable name must stop before any mutating call is issued."""
    _patch(monkeypatch, "get", [_row(7, "nightly")])
    posted = _patch(monkeypatch, "post", {})
    assert _sched.cmd_schedules_start("nope") == 1
    assert posted == {}  # no POST attempted
    assert "not found" in capsys.readouterr().err


# ── create ──


def test_create_posts_script_from_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "s.py"
    script.write_text("print('from file')\n")
    seen = _patch(monkeypatch, "post", _row(7, "nightly"), status=201)
    rc = _sched.cmd_schedules_create("nightly", None, str(script), None, "digest")
    assert rc == 0
    assert seen["url"] == "http://gw:8000/api/schedules"
    body = seen["json"]
    assert isinstance(body, dict)
    assert body["script"] == "print('from file')\n"
    assert body["name"] == "nightly"
    assert body["enabled"] is True
    assert "command" not in body  # unset -> the gateway's default applies


def test_create_disabled_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _patch(monkeypatch, "post", _row(7, enabled=False), status=201)
    rc = _sched.cmd_schedules_create(
        "nightly", "print(1)", None, "uv run x.py", None, disabled=True
    )
    assert rc == 0
    body = seen["json"]
    assert isinstance(body, dict)
    assert body["enabled"] is False
    assert body["command"] == "uv run x.py"


def test_create_requires_exactly_one_script_source(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Neither source, or both, is a clean rc=1 with no POST issued. The XOR is
    checked before the file is opened, so the both-given path needs no real file."""
    posted = _patch(monkeypatch, "post", {})
    assert _sched.cmd_schedules_create("n", None, None, None, None) == 1
    assert _sched.cmd_schedules_create("n", "print(1)", "s.py", None, None) == 1
    assert posted == {}
    assert "exactly one of --script / --script-file" in capsys.readouterr().err


def test_create_surfaces_syntax_error_detail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gateway compile()-checks the script; its 400 `detail` (with the line
    number) is the actionable part and must reach stderr, not a raw traceback."""
    _patch(
        monkeypatch,
        "post",
        {"detail": "script has a syntax error (line 3): invalid syntax"},
        status=400,
    )
    assert _sched.cmd_schedules_create("n", "def (", None, None, None) == 1
    assert "syntax error (line 3)" in capsys.readouterr().err


def test_unclassified_error_prints_the_body_before_raising(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A status the verb does not classify (422 body validation, 5xx) must still
    surface the gateway's reason — it fails loudly, but not silently."""
    _patch(monkeypatch, "post", {"detail": [{"loc": ["body", "name"], "msg": "too long"}]}, 422)
    with pytest.raises(httpx.HTTPStatusError):
        _sched.cmd_schedules_create("n", "print(1)", None, None, None)
    assert "too long" in capsys.readouterr().err


def test_create_name_clash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch(monkeypatch, "post", {"detail": "exists"}, status=409)
    assert _sched.cmd_schedules_create("nightly", "print(1)", None, None, None) == 1
    assert "already exists" in capsys.readouterr().err


# ── update ──


def test_update_sends_only_the_passed_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """PUT carries an exclude-unset body — an omitted flag must not be echoed as
    null, or the gateway would clobber the stored value."""
    seen = _patch(monkeypatch, "put", _row(7))
    rc = _sched.cmd_schedules_update("7", None, None, None, "uv run x.py", None, enabled=None)
    assert rc == 0
    assert seen["url"] == "http://gw:8000/api/schedules/7"
    assert seen["json"] == {"command": "uv run x.py"}


def test_update_enable_disable_is_tristate(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _patch(monkeypatch, "put", _row(7))
    _sched.cmd_schedules_update("7", None, None, None, None, None, enabled=False)
    assert seen["json"] == {"enabled": False}
    _sched.cmd_schedules_update("7", None, None, None, None, None, enabled=True)
    assert seen["json"] == {"enabled": True}


def test_update_with_no_fields_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    put = _patch(monkeypatch, "put", {})
    assert _sched.cmd_schedules_update("7", None, None, None, None, None, enabled=None) == 1
    assert put == {}
    assert "at least one of" in capsys.readouterr().err


def test_update_reads_script_from_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--script-file -` is the heredoc form; the body comes off stdin."""
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("print('piped')\n"))
    seen = _patch(monkeypatch, "put", _row(7))
    rc = _sched.cmd_schedules_update("7", None, None, "-", None, None, enabled=None)
    assert rc == 0
    assert seen["json"] == {"script": "print('piped')\n"}


# ── control / delete ──


@pytest.mark.parametrize("verb", ["start", "stop", "restart"])
def test_control_verbs_hit_their_subpath(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], verb: str
) -> None:
    seen = _patch(monkeypatch, "post", _row(7))
    assert getattr(_sched, f"cmd_schedules_{verb}")("7") == 0
    assert seen["url"] == f"http://gw:8000/api/schedules/7/{verb}"
    assert seen["json"] is None  # these routes take no body
    assert verb in capsys.readouterr().out


def test_restart_of_a_disabled_schedule_surfaces_the_409(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch(
        monkeypatch,
        "post",
        {"detail": "schedule is disabled; start it instead of restarting"},
        status=409,
    )
    assert _sched.cmd_schedules_restart("7") == 1
    assert "start it instead" in capsys.readouterr().err


def test_delete_force_skips_the_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _patch(monkeypatch, "delete", {"status": "deleted"})
    assert _sched.cmd_schedules_delete("7", force=True) == 0
    assert seen["url"] == "http://gw:8000/api/schedules/7"
    assert "deleted schedule #7" in capsys.readouterr().out


def test_delete_declined_at_the_prompt_issues_no_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("builtins.input", lambda _p: "n")  # pyright: ignore[reportUnknownArgumentType]
    deleted = _patch(monkeypatch, "delete", {})
    assert _sched.cmd_schedules_delete("7") == 0
    assert deleted == {}
    assert "cancelled" in capsys.readouterr().out


# ── observation ──


def test_logs_passes_the_line_count_and_labels_the_source(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _patch(monkeypatch, "get", {"source": "last_error", "lines": ["Traceback", "  boom"]})
    assert _sched.cmd_schedules_logs("7", 50) == 0
    assert seen["url"] == "http://gw:8000/api/schedules/7/logs"
    assert seen["params"] == {"lines": 50}
    out = capsys.readouterr().out
    assert "last_error" in out and "boom" in out


def test_logs_none_source_is_explained(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch(monkeypatch, "get", {"source": "none", "lines": []})
    assert _sched.cmd_schedules_logs("7", 200) == 0
    assert "no output yet" in capsys.readouterr().out


def test_runs_renders_history(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = _patch(
        monkeypatch,
        "get",
        [
            {"id": 2, "ran_at": "2026-07-02T01:00:00Z", "ok": True, "agent_id": 91, "note": "ok"},
            {"id": 1, "ran_at": "2026-07-01T01:00:00Z", "ok": None, "agent_id": None, "note": None},
        ],
    )
    assert _sched.cmd_schedules_runs("7", 10) == 0
    assert seen["params"] == {"limit": 10}
    out = capsys.readouterr().out
    assert "91" in out and "2026-07-02T01:00:00Z" in out


def test_runs_empty(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _patch(monkeypatch, "get", [])
    assert _sched.cmd_schedules_runs("7", 50) == 0
    assert "(no runs yet)" in capsys.readouterr().out


# ── parser wiring ──


def test_every_schedules_verb_is_registered() -> None:
    """The parser exposes one verb per /api/schedules route the CLI covers."""
    import argparse
    from typing import cast

    p = _build_parser()
    cmd = next(a for a in p._actions if a.dest == "cmd")
    schedules_p = cast("dict[str, argparse.ArgumentParser]", cmd.choices)["schedules"]
    sub = next(a for a in schedules_p._actions if a.dest == "schedules_cmd")
    assert set(cast("dict[str, object]", sub.choices)) == {
        "ls",
        "get",
        "create",
        "update",
        "delete",
        "provision",
        "start",
        "stop",
        "restart",
        "logs",
        "runs",
    }


def test_script_flags_are_mutually_exclusive() -> None:
    p = _build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["schedules", "create", "--name", "n", "--script", "x", "--script-file", "f"])


def test_enable_and_disable_are_mutually_exclusive() -> None:
    p = _build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["schedules", "update", "7", "--enable", "--disable"])


def test_provision_creates_builtins(
    db_conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ava schedules provision` creates the repo's built-in schedules in the
    DB (product enabled, operator disabled) and is a no-op on the second run."""
    assert _sched.cmd_schedules_provision() == 0
    out = capsys.readouterr().out
    assert "self-evolution-weekly" in out
    assert "memory-arbiter" in out
    assert "trace-ship-tempo" in out
    with db_conn.cursor() as cur:
        cur.execute("SELECT name, enabled FROM schedules ORDER BY name")
        rows = dict(cur.fetchall())
    assert rows["self-evolution-weekly"] is True
    assert rows["memory-arbiter"] is True
    assert rows["trace-ship-tempo"] is False

    # Second run: idempotent, nothing created.
    assert _sched.cmd_schedules_provision() == 0
    assert "(all built-in schedules already present)" in capsys.readouterr().out
