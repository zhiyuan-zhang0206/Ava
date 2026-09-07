---
type: doc
title: Incarnation resource evidence
description: Nullable server-owned resource evidence and conservative schema retirement.
---

# Incarnation resource evidence

`agents_meta.incarnation_resources` is separate from configuration. NULL means
unknown, not an empty resource set. The additive migration does not initialize
historical owners or grant resource admission. Actual admission must
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

Process and hosted admission preserve legacy NULL/protocol zero and refuse
malformed evidence. A managed successor requires a closed exact predecessor
resource set plus its applied lifecycle decision. A same-machine hosted process
restart is the narrow exception: while holding the metadata lock, it may transfer
an empty, unfrozen set only after the admission-captured host PID/birth is proven
ended. A live exact host, an expired lease, a missing host identity, a frozen set,
or any request refuses that handoff. No default spawn stamps the birth marker:
enabling new births still requires the publication/all-writer boundary, never an
environment flag or an installed revision.

Managed exec launches the fixed isolated read-only `agent.exec_domain_owner`
entry (`-I -B -X utf8`) behind a permit gate, validates its actual PID/birth and
direct root, and then registers and attaches the allocation atomically under the
metadata lock. Only that committed transaction permits user code. Force winning
before the transaction leaves no database allocation; the host closes the gated
owner and requires its exact `host_eof` receipt before clearing the in-process
scope. An ambiguous commit remains unresolved. The root is gated by
`agent.exec_owner_child`; after the permit it rechecks the reserved request digest
and exact request/result paths. Neither child inherits the host's control write
end. The managed host poll loop publishes pending output incrementally and sends
keepalives while the owner remains live; completion flushes only the unpublished
tail. Task cancellation closes the owner's control input but does not abandon an
in-flight registration transaction or an attached allocation. The caller retains
those tasks and consumes the exact terminal receipt before propagating cancellation;
cancellation cannot abandon a completion consumer before resource ownership
has been settled.
The owner remains alive after host EOF, closes the managed domain, reaps its
root, joins the output reader and exclusively publishes the terminal receipt.
Request files live in exact domain subdirectories outside legacy age pruning.
Missing/partial receipts and owner death without a receipt retain uncertainty.
The native domain and close-grace constant live in `shared.exec_process_domain`;
the owner never imports `agent.graph` or its eager SDK/plugin initialization.
Control is read nonblockingly by the owner loop itself, without a buffered-stdin
daemon that could hold an interpreter shutdown lock. Partial/oversized records
refuse; the original deadline still bounds reads. Windows roots enter the Job
atomically at creation, including venv redirectors; attaching a redirector after
its interpreter exists does not retroactively adopt that interpreter's children.

Existing local wake and admission paths recover only entries selected by the
complete DB map. They verify the exact immutable context, request digest,
owner/root receipt and ended owner before CAS discharge. Dead-host force
completion additionally requires the admission-captured host PID/birth to have
ended and the entire frozen set to be discharged. Same-user arbitrary code is
not sandboxed; the guarantee is registered managed-domain closure, not every
possible detached or breakaway process. Persistent sessions remain independent.

The dedicated owner's `ExecProcessDomain.close_confirmed` operation retains the
POSIX unreaped root while observing that no live managed group members remain.
It is distinct from successful signal submission. On Windows,
`WindowsJob.terminate_and_confirm` retains the original Job handle through
termination and a zero `ActiveProcesses` readback, then closes it. Query failure
or timeout remains unknown even if fallback close subsequently kills members.
Neither operation covers unregistered POSIX session escapes or Windows breakaway.
These stronger operations are used by the independent owner, never by a
historical numeric PGID after its direct-child pin has been released.

Windows accounting follows the native
[QueryInformationJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-queryinformationjobobject)
and [basic accounting](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information)
contracts; real native CI, not simulated handle close, must establish support.
