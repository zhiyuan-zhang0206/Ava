---
type: doc
title: Fixed-base legacy normal ops compatibility proof
description: Reconstruct a pinned legacy wheel without reverting the current application.
---

# Fixed-base legacy normal ops compatibility proof

`manifest.json` binds the actual `612326d` base, compatibility patch SHA256,
reconstructed Git tree and existing preparation-helper revision. CI creates a
separate detached base worktree and applies only that exact patch. The main PR's
synthetic merge is the harness, not the claimed legacy application. No legacy
business files are installed into current main by this PR.

The patch packages the full old Python application/resources, binds wheel home
before normal imports, separates retained plugin reads from mutable install
destinations, and replaces installed-only Git SQL enumeration with exact built
SQL hashes. Source checkout behavior and required/applied schema-set equality
stay unchanged. Packaged legacy migration/down/rollback writes explicitly refuse.
The generated SQL inventory is sealed into the wheel and bound by its hash;
it does not grant schema mutation or deployment authority.

Preparation uses standard locked uv/pip wheel inputs and the already reviewed
private CPython copy verification from the manifest's exact tool revision.
Downloads/build happen before source retirement. Offline private venv installation
uses copies, then the image is read-only. A temporary native PG database applies
the real old baseline/deltas. The retained interpreter invokes the actual old
normal ops `main()` after writing a same-process module/PID/native provenance
receipt; no startup/schema/readiness implementation is patched by the harness.

The cold proof requires actual normal readiness, exact native PID/birth/home,
normal registration, retained executable/stdlib, unknown `process_sha`, strict
wrong-home refusal and wrong applied-set/SQL-tamper refusal. Mutable plugin poison
must not enter discovery. After normal graceful stop, image byte inventory must
be unchanged. All config/log/queue writes remain under the private home.

The installed old envelope reader also exercises legacy sources and rejects the
exact persisted `external_agent:codex:run-42` fixture from identity revision
`d39ca01c155305f1e8ae504cf9f5ed1a0e0e8cc1`, plus `unknown:cli`. This records a
real readability incompatibility, not a rollback admission implementation.
Any retained inbound/checkpoint replay using that format must block old-version
rollback until a separately reviewed compatibility/retirement path exists;
rewriting the source label to make the old reader accept it is not that path.

CI has Linux x86_64 and macOS arm64 lanes, with actual architecture recorded and
the macOS runner explicitly checked. On macOS a proof-only observer thread in
the actual normal process waits for that PID's native health readiness, then
records the dyld image list; it does not replace daemon startup or readiness.
The parent matches PID/birth and only accepts retained-image or OS ABI paths.
This is not Windows normal-service proof, full enabled-service closure, a
selector commit or a production update. macOS loopback readiness does not prove
Tailnet reachability or authorize firewall/local-network approval changes.
Successful cold boot is not complete LKG recovery: actual down-migration safety,
persisted message/protocol readability, old orchestrator handoff and all managed
writer closure remain required. Correct refusal of a lossy down means automatic
legacy rollback is unavailable for that data state; a backup is not proof of it.
