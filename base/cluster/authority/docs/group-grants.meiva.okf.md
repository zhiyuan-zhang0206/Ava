---
type: doc
title: Database capability group grants
description: Database capability group grants.
tags: [contracts]
---

# Database capability group grants

The grant matrix in `groups.py` is the single runner authority. Entries use fixed
privilege strings and quoted table identifiers. Gateway receives owner-like DML,
sequence access, routine execution and checkpoint MAINTAIN, without ownership,
DDL, TRUNCATE, REFERENCES or TRIGGER. Audit and telemetry tables are append-only.
Runner grants follow actual direct writers; admission remains with Gateway where
the domain requires it. Projection tables permit SELECT, INSERT and DELETE.

Default privileges cover future owner-created objects; rerunning convergence
covers existing objects. Missing matrix tables fail loudly. Group validation
refuses LOGIN, elevated attributes, settings, ownership and foreign memberships;
it never repairs an unknown role into a trusted one. Sequence USAGE permits
BIGSERIAL writes, and the SECURITY DEFINER partition writer creates partitions.
VACUUM needs an autocommit connection and treats any skip warning as failure.
Task-receipt tables require explicit runner grants: their original omission
caused InsufficientPrivilege after they were introduced in October 2026.

```meiva-contract
id = "capability-grant-convergence"
kind = "invariant"
statement = "Group convergence is idempotent, does not change LOGIN, covers future tables and fails a VACUUM skip instead of reporting successful maintenance."
code = ["base/cluster/authority/groups.py:<module>"]
tests = ["base/deploy/tests/authority/test_catalog.py::test_default_privileges_cover_tables_created_after_birth", "base/deploy/tests/authority/test_catalog.py::test_ensure_groups_is_idempotent_and_never_changes_login", "base/deploy/tests/authority/test_catalog.py::test_gateway_vacuum_requires_maintain_and_a_skip_is_a_failure"]
```
