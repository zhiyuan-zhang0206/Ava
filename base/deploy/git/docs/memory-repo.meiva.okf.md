---
type: doc
title: Memory checkout operations
description: Memory checkout refresh, role-aware initialization and gateway bundle bootstrap.
tags: [base, memory, git]
---

# Memory checkout operations

`memory_repo.py` owns local Git operations, initialization and split-runner
bootstrap. Steward and arbiter agents own authoring, push/PR and consolidation;
this module does not schedule those responsibilities. Paths come from the
foundation path layer, avoiding a dependency on the SDK's memory facade.

## Refresh the reader's checkout

The gateway passes the same captured checkout that its memory reads use. It
reads keep-local and track-branch from its configuration owner for each refresh;
the Git operation does not rediscover machine roles, paths or global settings.
The legacy name `pull_main` does not restrict the branch to literal `main`.
The indexer independently observes checkout changes; it is not a caller of
`pull_main`. Refresh completion proves a checkout update, not embedding completion.

## Initialization and ownership

Runner authoring uses machine-<name>, including combined gateway/runner units. The consolidated gateway checkout uses main and a separate directory on combined units. Only a gateway-only authoring checkout may automatically leave a stale machine branch; runner work must not be switched implicitly. Keep-local initialization removes remotes.

Initialization's `subprocess.run` calls use argv lists with the Git executable
fixed; configured remote and checkout values remain individual operands, never
shell source. The existing S603 annotations preserve that reviewed call shape.
The named memory exceptions describe observable states and are consumed by CLI
and plugin setup; N818 annotations retain those existing names.

A fresh split runner without a remote bootstraps the gateway bundle or fails loudly; it never silently starts with an empty shared pool. Bundle ancestry and advertised HEAD are checked before installation, and the temporary bundle origin is removed. An existing initialized checkout is not re-bootstrapped.

The fresh checkout is prepared in a temporary directory and moved into place;
pre-init leftovers may be removed, but initialization never invokes this path
on an authored Git checkout. Gateway-capable first boot can initialize locally
because its gateway need not be serving yet. `# ambient: B2` still identifies
the bootstrap access reader for the exact caller inventory in
`base/cluster/machine/tests/test_ambient_callers.py`; refresh does not migrate
that separate dependency.

## Bounded I/O and status

`run_bounded` owns descendant cleanup; `subprocess.run(timeout=...)` alone
would only terminate its direct child. Bounding Git also bounds a refresh
worker that otherwise holds executor shutdown open. The existing bound is per
command, not one deadline for an entire fetch/merge sequence.

Pool downloads enforce both declared and streamed size limits and report transport failures as MemoryPoolBootstrapFailed, preventing an unbounded response from filling memory before installation.

Repository status renders FETCH_HEAD's modification time in the cluster time zone. The existing RepoStatus value and MemoryRepoUninitialized error remain the status surface; inspecting status does not initialize a repository.

`RepoStatus.is_clean` covers the working tree and index. The local commit count
is the entire HEAD ancestry; ahead/behind compare to the configured track
branch, despite the historical field names ending in `_main`. Without that
remote reference they remain zero. `last_fetch` renders FETCH_HEAD's mtime in
the cluster time zone, or `never`; an uninitialized repository raises
`MemoryRepoUninitialized` rather than reporting an empty repository.

Parent: [[base/deploy/docs/deploy.meiva.okf.md]].
