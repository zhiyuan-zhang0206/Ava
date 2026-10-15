import subprocess
import sys

if __name__ == "__main__":
    rows = sys.argv[1]
    command = [
        sys.executable,
        "-m",
        "cli.main",
        "schedules",
        "verify",
        "--rows-file",
        rows,
        "--no-notify",
    ]
    sys.exit(subprocess.run(command, check=False).returncode)
