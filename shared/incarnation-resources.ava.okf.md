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
resource set plus its applied lifecycle decision. Legacy spawn does not stamp the
birth marker: enabling new births requires the publication/all-writer
boundary, never an environment flag or an installed revision. The integrated
current-only spawn boundary stamps a fixed first-birth deadline and attempt
limit in that same record. Its existing process controller revisits exact birth
attempts after maintenance; missing prior native identity refuses another launch.
Counter allocation commits before Popen, and the actual Python checks the exact
birth UUID/attempt and original deadline before admission. Hosted first admission
consumes the same marker within its deadline without a process launch token.

Actual process admission resolves the loaded image once before the database
transaction. Hosted admission shares one boot-resolution task and rechecks its
cheap immutable binding without traversing the image on each turn. The existing
publication decision locks deployment and registry before agent metadata. Pending
publication exits a process before ownership or returns no hosted admission;
queued inbound is not consumed. Legacy generic unowned-boot terminal cleanup is
allowed only in the exact stable, never-enabled SQL-NULL publication state, not
after a deferred child exits. This does not renew a command's launch deadline.
Incomplete historical v2 publication without activation hash/challenge is not
new-mode permission. Historical NULL resource rows remain unknown under current
publication and cannot become empty through admission.

Managed exec calls reserve under the metadata lock, launch the fixed isolated
`agent.exec_domain_owner` entry, validate its actual PID/birth and direct root,
attach that allocation, and then send the exact permit. The root is gated by
`agent.exec_owner_child`; after the permit it rechecks the reserved request
digest and exact request/result paths. Neither child inherits the host's control write end.
The owner remains alive after host EOF, closes the managed domain, reaps its
root, joins the output reader and exclusively publishes the terminal receipt.
Request files live in exact domain subdirectories outside legacy age pruning.
Missing/partial receipts and owner death without a receipt retain uncertainty.
The native domain and close-grace constant live in `shared.exec_process_domain`;
the owner never imports `agent.graph` or its eager SDK/plugin initialization.

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
