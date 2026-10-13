"""Check step of the pre-update schedule verify: NEW's `ava schedules verify` over a rows file.

`cli.fleet_update` sends this file's text to a host as `python -c` and runs it from NEW's
throwaway worktree, where the working directory wins `sys.path`. It runs NEW's command line
(`python -m cli.main`) as a child process, never the verify command function directly, whose
keyword-only parameters change between versions; the command line is the stable contract.
Usage: `schedules_verify_check_rows.py ROWS_FILE`.
"""

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
