---
type: doc
title: Incarnation resource evidence
description: Nullable server-owned resource evidence and conservative schema retirement.
---

# Incarnation resource evidence

`agents_meta.incarnation_resources` is separate from configuration. NULL means
unknown, not an empty resource set. The additive migration does not initialize
historical owners or grant resource admission. A future actual admission must
establish ownership and positive retirement of its predecessor before creating
a complete empty set; a dead PID, expired lease or absent file is insufficient.

Registration and force acceptance must serialize on the same metadata row.
An exec must be registered before its user-code permit; once force freezes the
incarnation, no additional resource may be admitted. Exact request/domain
evidence, not a caller-selected list, controls discharge. Execution and force
deadlines are distinct, fixed authorities and cannot renew one another.

The paired down migration takes ACCESS EXCLUSIVE before checking evidence and
retains that lock through DROP. Only entirely NULL, never-enabled storage can
be removed. Every non-NULL value, including an empty map or malformed version,
refuses retirement. An empty map alone cannot prove that a live producer will
not write again. Used-state rollback therefore requires a separately verified
all-writer retirement operation; this slice provides no automatic evidence
clearing or legacy rollback permission.

The schema foundation alone neither closes process domains nor completes
dead-host force recovery. Runtime registration, closure receipts and their
consumers must be verified together before that claim is made.
