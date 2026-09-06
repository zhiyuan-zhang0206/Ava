"""The opt-in WSL anchor must not inherit interactive runner limitations."""

import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from scripts.render_wsl_boot_task import render_task

_INPUT = {
    "distribution": "Ubuntu-24.04",
    "linux_user": "linux-owner",
    "windows_user": r"HOST\windows-owner",
    "wsl_executable": r"C:\Windows\System32\wsl.exe",
}
_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def _parse(xml: str | bytes) -> ET.Element:
    return ET.fromstring(xml)  # noqa: S314 \u2014 generated locally, no external input


def test_no_logon_password_or_lifetime_limit() -> None:
    root = _parse(render_task(**_INPUT))
    assert root.findtext("t:Principals/t:Principal/t:LogonType", namespaces=_NS) == "S4U"
    assert (
        root.findtext("t:Principals/t:Principal/t:UserId", namespaces=_NS) == _INPUT["windows_user"]
    )
    assert root.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=_NS) == "LeastPrivilege"
    assert root.find("t:Triggers/t:LogonTrigger", _NS) is None
    assert root.findtext("t:Settings/t:ExecutionTimeLimit", namespaces=_NS) == "PT0S"
    for name in (
        "DisallowStartIfOnBatteries",
        "StopIfGoingOnBatteries",
        "RunOnlyIfIdle",
        "WakeToRun",
    ):
        assert root.findtext(f"t:Settings/t:{name}", namespaces=_NS) == "false"


def test_boot_repetition_recovers_any_exit_without_duplicate_anchors() -> None:
    root = _parse(render_task(**_INPUT))
    assert (
        root.findtext("t:Triggers/t:BootTrigger/t:Repetition/t:Interval", namespaces=_NS) == "PT1M"
    )
    assert root.find("t:Triggers/t:BootTrigger/t:Repetition/t:Duration", _NS) is None
    assert root.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=_NS) == "IgnoreNew"
    assert root.findtext("t:Settings/t:StartWhenAvailable", namespaces=_NS) == "true"
    assert root.find("t:Settings/t:RestartOnFailure", _NS) is None
    assert root.findtext("t:Actions/t:Exec/t:Command", namespaces=_NS) == _INPUT["wsl_executable"]
    assert root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=_NS) == (
        "--distribution Ubuntu-24.04 --user linux-owner --exec /usr/bin/sleep infinity"
    )


def test_xml_and_windows_arguments_are_escaped_separately() -> None:
    root = _parse(render_task(**(_INPUT | {"distribution": "Ubuntu & tools"})))
    assert root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=_NS) == (
        '--distribution "Ubuntu & tools" --user linux-owner --exec /usr/bin/sleep infinity'
    )


def test_xml_accepts_utf16_transport_used_by_windows_com() -> None:
    xml = render_task(**_INPUT)
    # TaskDefinition.XmlText receives a UTF-16 BSTR even when Python emits UTF-8.
    root = _parse(xml.encode("utf-16"))
    assert (
        root.findtext("t:Principals/t:Principal/t:UserId", namespaces=_NS) == _INPUT["windows_user"]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("distribution", ""),
        ("distribution", "Ubuntu\n--unregister"),
        ("distribution", "--unregister"),
        ("linux_user", " "),
        ("windows_user", "unqualified"),
        ("windows_user", "HOST\\"),
        ("windows_user", r"NT AUTHORITY\SYSTEM"),
        ("wsl_executable", "wsl.exe"),
        ("wsl_executable", r"\\server\share\wsl.exe"),
        ("wsl_executable", r"C:\Windows\System32\cmd.exe"),
    ],
)
def test_invalid_identity_or_executable_fails_before_rendering(field: str, value: str) -> None:
    with pytest.raises(ValueError):
        render_task(**(_INPUT | {field: value}))


@pytest.mark.parametrize("windows_user", [r"HOST\windows-owner", "HOST\\\u6d4b\u8bd5\u7528\u6237"])
def test_standalone_cli_needs_no_ava_install_or_runtime(tmp_path: Path, windows_user: str) -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "render_wsl_boot_task.py"
    inputs = _INPUT | {"windows_user": windows_user}
    args = [part for key, value in inputs.items() for part in ("--" + key.replace("_", "-"), value)]
    result = subprocess.run(  # noqa: S603 \u2014 fixed local script and fixture arguments
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import runpy,sys; sys.stdout.reconfigure(encoding='ascii'); "
            "sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')",
            str(script),
            *args,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    root = _parse(result.stdout)
    assert root.findtext("t:Principals/t:Principal/t:UserId", namespaces=_NS) == windows_user
    assert list(tmp_path.iterdir()) == []
    assert result.stderr == ""
