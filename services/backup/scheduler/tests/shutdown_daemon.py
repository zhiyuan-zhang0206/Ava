import sys
from pathlib import Path

from services.backup.scheduler.tests.test_scheduler_shutdown import exercise_daemon

if __name__ == "__main__":
    exercise_daemon(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]))
