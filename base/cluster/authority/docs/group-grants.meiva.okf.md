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
