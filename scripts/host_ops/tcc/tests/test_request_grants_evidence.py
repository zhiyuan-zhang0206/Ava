from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.host.permissions_helper.wire import RequestGrantsResult
from scripts.host_ops.tcc import request_grants_evidence as module


def no_log(_seconds: int) -> str:
    return ""


def one_log(_seconds: int) -> str:
    return LINE


LINE = "2026-10-17 17:40:00 tccd[120]: AUTHREQ_RESULT kTCCServiceScreenCapture authValue=2"


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
        self, name: str, argv: list[str], env: dict[str, str], cwd: str, stdout: str, stderr: str
    ) -> dict[str, object]:
        raise AssertionError(f"no child expected: {name} {argv} {env} {cwd} {stdout} {stderr}")


def test_only_the_two_grant_decisions_are_evidence() -> None:
    output = "\n".join(
        [
            "2026-10-17 17:40:00 tccd[120]: AUTHREQ_RESULT authValue=2 service=kTCCServiceScreenCapture",
            "2026-10-17 17:40:00 tccd[120]: something else entirely",
            "2026-10-17 17:40:01 tccd[120]: AUTHREQ result for kTCCServiceAccessibility",
        ]
    )
    assert module.grant_log_lines(output) == [output.splitlines()[0], output.splitlines()[2]]
    assert module.grant_log_lines("no decisions here") == []


def test_a_host_that_is_not_macos_is_a_setup_failure(tmp_path: Path) -> None:
    assert module.main(["--out", str(tmp_path)], platform="linux", log_reader=no_log) == 2


def test_an_old_helper_build_is_a_setup_failure(tmp_path: Path) -> None:
    helper = FakeHelper(capable=False)
    assert (
        module.main(["--out", str(tmp_path)], client=helper, platform="darwin", log_reader=no_log)
        == 2
    )
    assert helper.requests == 0


def test_the_evidence_names_what_was_requested_and_what_tccd_said(tmp_path: Path) -> None:
    helper = FakeHelper()
    line = "2026-10-17 17:40:00 tccd[120]: AUTHREQ_RESULT kTCCServiceScreenCapture authValue=2"
    assert (
        module.main(
            ["--out", str(tmp_path)],
            client=helper,
            platform="darwin",
            log_reader=lambda _seconds=0: line,
        )
        == 0
    )
    assert helper.requests == 1
    files = sorted(tmp_path.glob("request-*.json"))
    assert len(files) == 1
    body = json.loads(files[0].read_text())
    assert body["requested"] == {"screen_recording": False, "accessibility": False}
    assert body["tccd_lines"] == [line]
    assert body["helper_pid"] == 42


def test_a_request_without_a_decision_line_is_not_evidence(tmp_path: Path) -> None:
    assert (
        module.main(
            ["--out", str(tmp_path)],
            client=FakeHelper(),
            platform="darwin",
            log_reader=no_log,
        )
        == 1
    )


def test_a_dead_helper_is_a_setup_failure(tmp_path: Path) -> None:

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

    assert (
        module.main(["--out", str(tmp_path)], client=Dead(), platform="darwin", log_reader=no_log)
        == 2
    )


if __name__ == "__main__":
    pytest.main([__file__])
