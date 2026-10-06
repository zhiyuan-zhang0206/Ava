"""Disposable scheduled worker entry for real signal and Postgres tests."""

import os
from pathlib import Path

from services.backup.scheduler.tests.test_scheduler_shutdown import _exercise_job

if __name__ == "__main__":
    _exercise_job(
        Path(os.environ["AVA_TEST_BACKUP_ROOT"]),
        os.environ["AVA_TEST_BACKUP_MODE"],
        Path(os.environ["AVA_TEST_BACKUP_POSTGRES_BASE"]),
    )
