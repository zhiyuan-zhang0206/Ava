---
type: doc
title: Database isolation inventory
description: Authoritative schema inventory and live cascade evidence in test cleanup checks.
---

# Database isolation inventory

`tests/contracts/test_lint_truncate_isolation.py` builds authoritative table
names from the schema and ordered migration replay. A live xdist database can
also contain transient tables created by peers, so live inventory alone is not
that authority. Live foreign-key edges supply the separate cascade closure.
SQL comments must be stripped before recognizing DDL; prose is not a table
creation. The check combines the truncate list, cascade closure and explicit
exemption reasons, whose executable fixture values remain intact.
