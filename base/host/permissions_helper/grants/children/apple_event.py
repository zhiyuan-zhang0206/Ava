import subprocess
import sys
from pathlib import Path

OSASCRIPT = "/usr/bin/osascript"


def main() -> None:
    script, result_path = sys.argv[1], Path(sys.argv[2])
    try:
        done = subprocess.run(
            [OSASCRIPT, "-e", script], capture_output=True, text=True, check=False
        )
        if done.returncode == 0:
            text = "granted " + done.stdout.strip()[:120]
        else:
            text = "failed " + done.stderr.strip()[:200]
    except Exception as exc:
        text = f"error {type(exc).__name__}: {exc}"
    with result_path.open("w") as handle:
        handle.write(text)


if __name__ == "__main__":
    main()
