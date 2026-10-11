"""Child process: a read-only attachment with an undelivered login is refused before any dial."""

import os
from unittest.mock import patch

from ava import external
from base.db import NoDatabaseAuthorityError

before = dict(os.environ)
with patch("psycopg.connect", side_effect=AssertionError("native dial must not run")):
    try:
        external.attach("lease")
    except NoDatabaseAuthorityError as error:
        assert "startup entry" in str(error)
    else:
        raise AssertionError("undelivered login must be refused")
assert dict(os.environ) == before
