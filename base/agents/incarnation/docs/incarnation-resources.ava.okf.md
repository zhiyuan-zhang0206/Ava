---
type: doc
title: Incarnation resource evidence
description: Nullable server-owned resource evidence and conservative schema retirement.
---

# Incarnation resource evidence

`agents_meta.incarnation_resources` is separate from configuration. NULL means
unknown, not an empty resource set. The additive migration does not initialize
historical owners or grant resource admission. Actual admission must
establish ownership and close any recorded exec allocations before creating
a complete empty set; a dead PID, expired lease or absent file cannot discharge
an exec allocation.

Registration and force acceptance must serialize on the same metadata row.
An exec must be registered before its user-code permit; once force freezes the
incarnation, no additional resource may be admitted. Exact request/domain
evidence, not a caller-selected list, controls discharge. Execution and force
deadlines are distinct, fixed authorities and cannot renew one another.

A retirement migration takes ACCESS EXCLUSIVE before checking evidence and
retains that lock through DROP. Only entirely NULL, never-enabled storage can
be removed. Every non-NULL value, including an empty map or malformed version,
refuses retirement. An empty map alone cannot prove that a live producer will
not write again. Used-state rollback therefore requires a separately verified
all-writer retirement operation; this slice provides no automatic evidence
clearing or legacy rollback permission.

Process and hosted admission preserve legacy NULL/protocol zero and refuse
malformed evidence. The metadata row owns runtime admission: once its generation,
owner, kind and per-agent PID are released, a successor may transfer the empty
resource set under the same row lock as its ownership claim. Admission does not
query historical restart or terminate receipts. A failed model turn can be reaped
and resurrected in the same live shared host; it needs no synthetic lifecycle
receipt and no host restart. Resurrection refuses unresolved exec allocations
before releasing the identity, so exact receipt recovery retains its target.

An unreleased managed owner can be replaced only when its admission-captured
host native birth is proven ended, with an empty, unfrozen resource set. A live
host, an expired lease alone, a missing host identity, a frozen set, or any request
refuses that takeover. A legacy NULL row keeps its unknown set
(admission never mints an empty one) and is replaced only after its lease
expires; NULL evidence never authorizes an earlier takeover. Fresh `base.agents.birth.transaction.insert_agent_birth` metadata INSERTs stamp
one server-owned `ResourceBirth` in the same birth transaction. Real hosted
admission consumes it and captures its own host process before advertising
managed work. Receipt replay never refreshes resource evidence; historical NULL
rows remain unknown. An environment flag or installed revision is not evidence.

Values the current model cannot decode, the closed-predecessor form, drained
sets and never-admitted resurrection:
[[closed-predecessor.ava.okf.md|closed predecessor]].

Hosted admission locks only the agent's own metadata row and reads no
deployment-wide state. A stored resource value the current model cannot decode
refuses under every admission (`resource_fence`). Historical NULL resource rows
remain unknown and cannot become empty through admission.

Admission runs under the least-privilege `ava_runner` database identity, which
holds no write privilege on `deployment_state`.

Managed exec launches the fixed isolated read-only `agent.execution.domain_owner`
entry (`-I -B -X utf8`) behind a permit gate, validates its captured launcher birth and
direct root, and then registers and attaches the allocation atomically under the
metadata lock. Only that committed transaction permits user code. Force winning
before the transaction leaves no database allocation; the host closes the gated
owner and requires its exact `host_eof` receipt before clearing the in-process
scope. An ambiguous commit remains unresolved. The root is gated by
`agent.execution.owner_child`; after the permit it rechecks the reserved request digest
and exact request/result paths. Neither child inherits the host's control write
end. The managed host poll loop publishes pending output incrementally and sends
keepalives while the owner remains live; completion flushes only the unpublished
tail. Task cancellation closes the owner's control input but does not abandon an
in-flight registration transaction or an attached allocation. The caller retains
those tasks and consumes the exact terminal receipt before propagating cancellation;
cancellation cannot abandon a completion consumer before resource ownership
has been settled.
The gated child's independent deadline and normal stop/result ownership:
[[../../../../agent/execution/docs/gated-deadline.ava.okf.md|gated child deadline]].
The owner remains alive after host EOF, closes the managed domain, reaps its
root, joins the output reader and exclusively publishes the terminal receipt.
Request files live in exact domain subdirectories outside legacy age pruning.
Missing/partial receipts and owner death without a receipt retain uncertainty.
The native domain and close-grace constant live in `base.native_process.exec_domain`;
the owner never imports `agent.graph` or its eager SDK/plugin initialization.
Control is read nonblockingly by the owner loop itself, without a buffered-stdin
daemon that could hold an interpreter shutdown lock. Partial/oversized records
refuse; the original deadline still bounds reads.

Existing local wake and admission paths recover only entries selected by the
complete DB map. They verify the exact immutable context, request digest,
owner/root receipt and ended owner before CAS discharge. Dead-host force
completion additionally requires the admission-captured host PID/birth to have
ended and the entire frozen set to be discharged. Same-user arbitrary code is
not sandboxed; the guarantee is registered managed-domain closure, not every
possible detached or breakaway process. Persistent sessions remain independent.

Every `ResourceProcess` receipt requires explicit `starttime` and `boot_id`
fields. Local Linux custody requires PID, exact kernel start ticks, and boot
UUID; its wall-derived creation timestamp is diagnostic. macOS requires its
boot UUID and exact stable kernel birth. Missing historical
native fields refuse; they cannot become evidence that a host ended. A different
recorded boot or exact native PID reuse proves the prior process ended without
authorizing work on its replacement. Failed identity observation remains unknown.
Fresh host and ready-handshake captures compare native births; database CAS and
terminal receipts retain exact model equality, including the original timestamp.
Same-incarnation admission preserves the stored receipt rather than rewriting its facts.

Owner domain closure:
[[domain-closure.ava.okf.md|domain closure]].

Hosted admission retains the exact owner and resource fences. It does not
produce a cross-generation work-transfer certificate or recover cancellation.
Guarded work identity and cancellation:
[[native-work-cancel.ava.okf.md|native work cancellation]].
