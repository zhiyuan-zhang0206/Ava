import os
from unittest.mock import patch

from base.db import NoDatabaseAuthorityError
from meiva import impersonation

before = dict(os.environ)
with patch("psycopg.connect", side_effect=AssertionError("native dial must not run")):
    try:
        impersonation.attach("lease")
    except NoDatabaseAuthorityError as error:
        assert "startup entry" in str(error)
    else:
        raise AssertionError("undelivered login must be refused")
assert dict(os.environ) == before
