---
type: doc
title: Test database admission
description: Database admission and thread-local dial evidence in tests.
---

# Test database admission

`test_db_guard.py` is a safety helper despite its filename. A harness rooted
outside this repository can miss the root conftest and resolve the operator's
configuration. Database admission therefore cannot rely on fixture bootstrap
alone. `assert_test_db_url` rejects the historical `ava_main` name or a hostname
containing it even with `AVA_TEST_DB=1`. The fresh-birth `ava` name and other
non-test names require that explicit intent marker; throwaway name prefixes and
unreachable sentinel names are deliberate accepted cases. Missing URLs fail.

The dial recorders in `tests/test_database_handle.py` and `tests/test_db.py`
filter the owning thread. Their `psycopg.connect` patch affects the whole
process; an unrelated thread in the same worker must not insert the first dial
and falsify indexed kwargs assertions.
