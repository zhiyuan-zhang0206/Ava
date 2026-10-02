---
type: doc
title: Exec domain closure
description: How the dedicated exec owner proves its domain closed on POSIX and Windows, and what remains unknown.
tags:
- base
- exec
- process
---

# Exec domain closure

The dedicated owner's `ExecProcessDomain.close_confirmed` keeps the POSIX root
unreaped until [[../../process-group-closure.ava.okf.md|group closure]] is proven.
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
