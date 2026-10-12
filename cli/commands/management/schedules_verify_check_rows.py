"""Check step of the pre-update schedule verify: NEW's `ava schedules verify` over a rows file.

`cli.fleet_update` sends this file's text to a host as `python -c` and runs it from NEW's
throwaway worktree, where the working directory wins `sys.path`. It goes through the `cli.main`
entry point, never the verify command function directly, whose keyword-only parameters change
between versions. Usage: `schedules_verify_check_rows.py ROWS_FILE`.
"""

import sys

from cli.main import main

if __name__ == "__main__":
    rows = sys.argv[1]
    sys.argv = ["ava", "schedules", "verify", "--rows-file", rows, "--no-notify"]
    sys.exit(main())
