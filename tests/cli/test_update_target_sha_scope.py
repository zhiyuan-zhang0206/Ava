"""`ava cluster update --target-sha` — scoped to the per-host `--target` path.

History: the flag was removed for the whole-cluster verb (issue #216) — the
rollout contract resolves origin/main once and Phase B threads that single
commit to each runner over the OPS channel, so a CLI pin could only strand a
half-updated cluster. It was re-added (2026-09-11) as the **per-host**
primitive's pin: `--target-sha` is only meaningful together with `--target`,
where the gateway relay `POST /api/cluster/update?target=<m>&target_sha=<sha>`
forwards it to the target's own detached updater — the same pin a watchdog
off-pin self-heal passes, so an excluded host converges to exactly the cluster
pin instead of the moving tip (the mixed-version bootstrap chase). The file's
own rule made this a deliberate contract change rather than a silent
resurrection; these tests pin the NEW scope:

- `--target-sha` without `--target` is refused loudly (exit 2), never ignored;
- the pin rides only the target-scoped dispatch (and hence only its request);
- the whole-cluster rollout dispatch and its POST body stay pin-free.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from cli import main as _main


def test_target_sha_requires_target(capsys: pytest.CaptureFixture[str]) -> None:
    """`ava cluster update --target-sha <sha>` alone — a loud exit-2 refusal,
    not a silent-ignore window."""
    assert _main.main(["cluster", "update", "--target-sha", "0123456789abcdef"]) == 2
    assert "--target-sha requires --target" in capsys.readouterr().err


def test_target_sha_with_local_still_requires_target(capsys: pytest.CaptureFixture[str]) -> None:
    """The `--local` escape hatch does not make the pin meaningful on its own —
    the in-process orchestration resolves its own target like the detached one."""
    assert _main.main(["cluster", "update", "--local", "--target-sha", "0123456789abcdef"]) == 2
    assert "--target-sha requires --target" in capsys.readouterr().err


def test_target_scoped_dispatch_carries_the_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pin reaches `cmd_update` only together with `--target`."""
    import cli.commands as _commands

    seen: dict[str, object] = {}

    def _fake(**kw: object) -> int:
        seen.update(kw)
        return 0

    monkeypatch.setattr(_commands, "cmd_update", _fake)

    rc = _main.main(["cluster", "update", "--target", "macmini", "--target-sha", "abc123"])

    assert rc == 0
    assert seen["target"] == "macmini"
    assert seen["target_sha"] == "abc123"


def test_plain_update_dispatch_stays_pin_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain `ava cluster update` parses fine and carries no pin on any rung."""
    import cli.commands as _commands

    seen: list[dict[str, object]] = []

    def _fake(**kw: object) -> int:
        seen.append(kw)
        return 0

    monkeypatch.setattr(_commands, "cmd_update", _fake)

    assert _main.main(["cluster", "update"]) == 0
    assert seen and seen[0]["target"] is None and seen[0]["target_sha"] is None


def test_whole_cluster_rollout_body_stays_pin_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rollout POST body must not carry a pin — the gateway resolves the
    target once and Phase B threads it, never this CLI verb."""
    monkeypatch.setattr("shared.machine.gateway_api_base", lambda: "http://gw:8000")
    seen: dict[str, object] = {}

    class _Accepted:
        status_code = 202

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"session": "ava-rollout", "log": "rollout.log"}

    def fake_post(url: str, **kwargs: object) -> _Accepted:
        seen["json"] = kwargs.get("json")
        return _Accepted()

    monkeypatch.setattr("httpx.post", fake_post)

    from cli.commands import cmd_update

    assert cmd_update() == 0
    body = seen["json"]
    assert isinstance(body, dict)
    assert "target_sha" not in cast("dict[str, Any]", body)


def test_rollout_log_flag_is_internal_but_parseable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = _main._build_parser()
    log_path = "/home/ava/.ava/logs/rollout-1785470000.log"

    args = parser.parse_args(["cluster", "update", "--rollout-log", log_path])

    assert args.rollout_log == log_path
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["cluster", "update", "--help"])
    assert exc.value.code == 0
    assert "--rollout-log" not in capsys.readouterr().out


def test_rollout_log_flag_reaches_command_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    import cli.commands as _commands

    seen: dict[str, object] = {}
    log_path = "/home/ava/.ava/logs/rollout-1785470000.log"

    def _fake(**kw: object) -> int:
        seen.update(kw)
        return 0

    monkeypatch.setattr(_commands, "cmd_update", _fake)

    assert _main.main(["cluster", "update", "--local", "--rollout-log", log_path]) == 0
    assert seen["rollout_log"] == log_path
