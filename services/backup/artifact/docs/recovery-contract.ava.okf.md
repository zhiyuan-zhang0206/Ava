---
type: doc
title: Backup recovery source contract
description: The static test that consumes production recovery documentation.
---

# Backup recovery source contract

`tests/components/services/test_backup_recovery_contract.py` reads the module
docstrings of `services/backup/dump.py` and `base/agents/history/checkpoint.py`,
plus dump source and `db/schema.sql`. These are actual test inputs: migrating
their prose requires changing this consumer in the same contribution.

Its AST/source assertions keep checkpoint conversation storage, full-dump scope
and the retired events archive consistent. They do not execute a dump or restore;
the restore drill described by [[backup.ava.okf.md|backup]] owns that proof.
