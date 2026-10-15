import subprocess
from pathlib import Path

CHILD = Path(__file__).with_name("child_original_owner.py")


def child_failure(child: subprocess.Popen[str]) -> str:
    return child.stderr.read() if child.stderr is not None else "child did not return work"
