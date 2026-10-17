from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.host.permissions_helper.wire import RequestGrantsResult
from scripts.host_ops.tcc import request_grants_evidence as module

REQUEST = (
    "2026-10-17 18:00:00.000001+0800 tccd[120]: AUTHREQ_CTX: msgID=74954.1,"
    " function=TCCAccessRequest, service=kTCCServiceScreenCapture, preflight=no, query=1"
)
VERDICT = (
    "2026-10-17 18:00:01.123456+0800 tccd[120]: AUTHREQ_RESULT: msgID=74954.1,"
    " authValue=2, authReason=4"
)
PREFLIGHT = (
    "2026-10-17 18:00:00.500000+0800 tccd[120]: AUTHREQ_CTX: msgID=1.1,"
    " function=TCCAccessPreflight, service=kTCCServiceAccessibility, preflight=yes"
)
PREFLIGHT_VERDICT = (
    "2026-10-17 18:00:00.600000+0800 tccd[120]: AUTHREQ_RESULT: msgID=1.1,"
    " authValue=2, authReason=4"
)


def no_log(_start: datetime, _window: int) -> str:
    return ""


def decision_log(_start: datetime, _window: int) -> str:
    return "\n".join([REQUEST, PREFLIGHT, VERDICT, PREFLIGHT_VERDICT])


class FakeHelper:
    def __init__(self, *, capable: bool = True) -> None:
        self.capable = capable
        self.requests = 0

    def ping(self) -> dict[str, object]:
        return {"pong": True, "pid": 42, "request_grants_v1": self.capable}

    def request_grants(self) -> RequestGrantsResult:
        self.requests += 1
        return {"screen_recording": False, "accessibility": False}

    def spawn_process(
        self,
        name: str,
        argv: list[str],
        env: dict[str, str],
        cwd: str,
        stdout: str,
        stderr: str,
    ) -> dict[str, object]:
        raise AssertionError(f"no child expected: {name} {argv} {env} {cwd} {stdout} {stderr}")


class Dead:
    def ping(self) -> dict[str, object]:
        raise RuntimeError("socket refused")

    def request_grants(self) -> RequestGrantsResult:
        raise AssertionError("a dead helper is never asked")

    def spawn_process(
        self,
        name: str,
        argv: list[str],
        env: dict[str, str],
        cwd: str,
        stdout: str,
        stderr: str,
    ) -> dict[str, object]:
        raise AssertionError(f"no child expected: {name} {argv} {env} {cwd} {stdout} {stderr}")


def test_a_request_is_its_ctx_line_plus_the_verdict_with_the_same_msg_id() -> None:
    lines, decided = module.decision_evidence("\n".join([REQUEST, PREFLIGHT, VERDICT]))
    assert lines == [REQUEST, VERDICT]
    assert decided == {"kTCCServiceScreenCapture"}


def test_a_preflight_never_counts_even_though_it_also_has_a_verdict() -> None:
    lines, decided = module.decision_evidence("\n".join([PREFLIGHT, PREFLIGHT_VERDICT]))
    assert lines == []
    assert decided == set()


def test_a_request_without_a_verdict_is_not_evidence_yet() -> None:
    lines, decided = module.decision_evidence(REQUEST)
    assert lines == []
    assert decided == set()


def test_an_unrelated_line_never_becomes_evidence() -> None:
    lines, decided = module.decision_evidence("no decisions here")
    assert lines == []
    assert decided == set()


def test_the_log_is_read_from_the_request_window_only() -> None:
    seen: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")

    module.tccd_log(datetime(2026, 10, 17, 10, 0, tzinfo=UTC), 30, run=run)
    argv = seen[0]
    assert argv[:5] == ["log", "show", "--start", argv[3], "--end"]
    assert argv[5] > argv[3]
    assert 'process == "tccd"' in argv


def test_a_host_that_is_not_macos_is_a_setup_failure(tmp_path: Path) -> None:
    assert module.main(["--out", str(tmp_path)], platform="linux", log_reader=no_log) == 2


def test_an_old_helper_build_is_a_setup_failure(tmp_path: Path) -> None:
    helper = FakeHelper(capable=False)
    code = module.main(
        ["--out", str(tmp_path)], client=helper, platform="darwin", log_reader=no_log
    )
    assert code == 2
    assert helper.requests == 0


def test_the_evidence_names_what_was_requested_and_what_tccd_said(tmp_path: Path) -> None:
    helper = FakeHelper()
    code = module.main(
        ["--out", str(tmp_path)], client=helper, platform="darwin", log_reader=decision_log
    )
    assert code == 0
    assert helper.requests == 1
    files = sorted(tmp_path.glob("request-*.json"))
    assert len(files) == 1
    body = json.loads(files[0].read_text())
    assert body["requested"] == {"screen_recording": False, "accessibility": False}
    assert body["tccd_lines"] == [REQUEST, VERDICT]
    assert body["decided_services"] == ["kTCCServiceScreenCapture"]
    assert body["helper_pid"] == 42


def test_a_request_without_a_decision_line_is_not_evidence(tmp_path: Path) -> None:
    code = module.main(
        ["--out", str(tmp_path)], client=FakeHelper(), platform="darwin", log_reader=no_log
    )
    assert code == 1


def test_screen_recording_acceptance_needs_its_own_line(tmp_path: Path) -> None:
    class AccessOnly:
        def __call__(self, _start: datetime, _window: int) -> str:
            return "\n".join(
                [
                    PREFLIGHT.replace("preflight=yes", "preflight=no"),
                    PREFLIGHT_VERDICT,
                ]
            )

    code = module.main(
        ["--out", str(tmp_path), "--require", "kTCCServiceScreenCapture"],
        client=FakeHelper(),
        platform="darwin",
        log_reader=AccessOnly(),
    )
    assert code == 1


def test_a_dead_helper_is_a_setup_failure(tmp_path: Path) -> None:
    code = module.main(
        ["--out", str(tmp_path)], client=Dead(), platform="darwin", log_reader=no_log
    )
    assert code == 2


if __name__ == "__main__":
    pytest.main([__file__])
