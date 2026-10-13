"""Database handle behind a refused config boot; never imported by a production entry.

Run as ``python -B _boot_authority_child.py`` with the boot mode selected by the environment.
"""

from __future__ import annotations

import sys

import base.config as c
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.connections import NoDatabaseAuthorityError
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit

image = LoadedCommit.capture()
gate = ProcessDbGate(process="boot-authority-test", version=CodeVersion(image).get)
db = Database.from_settings(gate=gate)
assert c.settings.env_boot.db_authority_refusal is not None
assert "env_boot" not in c.settings.model_dump()
try:
    db.connect()
except NoDatabaseAuthorityError as exc:
    sys.stdout.write(f"REFUSED {exc}\n")
else:
    raise AssertionError("refused boot reached the database")
