import os
from pathlib import Path

from services.backup.scheduler.tests.test_scheduler_shutdown import exercise_job

if __name__ == "__main__":
    exercise_job(
        Path(os.environ["MEIVA_TEST_BACKUP_ROOT"]),
        os.environ["MEIVA_TEST_BACKUP_MODE"],
        Path(os.environ["MEIVA_TEST_BACKUP_POSTGRES_BASE"]),
    )
