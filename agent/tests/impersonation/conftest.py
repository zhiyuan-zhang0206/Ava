"""Real process evidence for takeover after the original host exited."""

import subprocess
import sys

import psutil
import pytest

from base.agents.incarnation.resources import ResourceProcess


@pytest.fixture
def exited_host() -> ResourceProcess:
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        identity = ResourceProcess.capture(psutil.Process(process.pid))
    finally:
        process.kill()
        process.wait(timeout=5)
    return identity
