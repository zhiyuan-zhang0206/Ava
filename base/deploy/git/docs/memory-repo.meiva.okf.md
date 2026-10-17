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

```meiva-contract
id = "refresh-checkout"
kind = "invariant"
statement = "pull_main fast-forwards the supplied root from origin/<branch> and returns its HEAD. keep_local only reads HEAD, including when no remote or requested branch exists. Divergence fails without discarding local commits."
code = ["base/deploy/git/memory_repo.py:pull_main"]
tests = ["base/deploy/git/tests/test_memory_repo.py::test_pull_main_git_sequence_and_returned_sha", "base/deploy/git/tests/test_memory_repo.py::test_pull_main_rejects_divergence", "base/deploy/git/tests/test_memory_repo.py::test_pull_main_keep_local_is_noop"]
```

The gateway passes the same captured checkout that its memory reads use. It
reads keep-local and track-branch from its configuration owner for each refresh;
the Git operation does not rediscover machine roles, paths or global settings.
The legacy name `pull_main` does not restrict the branch to literal `main`.
The indexer independently observes checkout changes; it is not a caller of
`pull_main`. Refresh completion proves a checkout update, not embedding completion.

## Initialization and ownership

```meiva-contract
id = "checkout-roles"
kind = "invariant"
statement = "Runner authoring uses machine-<name>, including combined gateway/runner units. The consolidated gateway checkout uses main and a separate directory on combined units. Only a gateway-only authoring checkout may automatically leave a stale machine branch; runner work must not be switched implicitly. Keep-local initialization removes remotes."
code = ["base/deploy/git/memory_repo.py:branch_name", "base/deploy/git/memory_repo.py:_reconcile_initialized", "base/deploy/git/memory_repo.py:init", "base/deploy/git/memory_repo.py:init_gateway", "base/deploy/git/memory_repo.py:init_local_repo", "base/deploy/git/memory_repo.py:MemoryBranchMismatch"]
tests = ["base/deploy/git/tests/test_memory_repo.py::test_branch_name_combined_unit_returns_machine_branch", "base/deploy/git/tests/test_memory_repo.py::test_init_gateway_on_stale_machine_branch_switches_to_main", "base/deploy/git/tests/test_memory_repo.py::test_init_combined_unit_does_not_auto_switch_agent_runner_checkout", "base/deploy/git/tests/test_memory_repo.py::test_gateway_memory_dir_combined_uses_subdirectory", "base/deploy/git/tests/test_memory_repo.py::test_init_keep_local_strips_remote_when_branch_correct", "base/deploy/git/tests/test_memory_repo.py::test_init_gateway_keep_local_strips_remote", "base/deploy/git/tests/test_memory_repo.py::test_init_keep_local_fresh_repo_inits_without_remote"]
```

Initialization's `subprocess.run` calls use argv lists with the Git executable
fixed; configured remote and checkout values remain individual operands, never
shell source. The existing S603 annotations preserve that reviewed call shape.
The named memory exceptions describe observable states and are consumed by CLI
and plugin setup; N818 annotations retain those existing names.

```meiva-contract
id = "split-runner-bootstrap"
kind = "invariant"
statement = "A fresh split runner without a remote bootstraps the gateway bundle or fails loudly; it never silently starts with an empty shared pool. Bundle ancestry and advertised HEAD are checked before installation, and the temporary bundle origin is removed. An existing initialized checkout is not re-bootstrapped."
code = ["base/deploy/git/memory_repo.py:bootstrap_from_gateway", "base/deploy/git/memory_repo.py:_unpack_bundle", "base/deploy/git/memory_repo.py:_init_without_remote", "base/deploy/git/memory_repo.py:MemoryPoolBootstrapFailed", "base/deploy/git/memory_repo.py:MemoryRemoteMissing"]
tests = ["base/deploy/git/tests/test_memory_repo_bootstrap.py::test_init_split_runner_without_remote_bootstraps_from_gateway", "base/deploy/git/tests/test_memory_repo_bootstrap.py::test_init_split_runner_bootstrap_failure_raises_loud", "base/deploy/git/tests/test_memory_repo_bootstrap.py::test_bootstrap_from_gateway_clones_bundle_as_machine_branch", "base/deploy/git/tests/test_memory_repo_bootstrap.py::test_bootstrap_from_gateway_head_mismatch_fails_loud", "base/deploy/git/tests/test_memory_repo_bootstrap.py::test_bootstrap_from_gateway_corrupt_bundle_fails_loud"]
```

The fresh checkout is prepared in a temporary directory and moved into place;
pre-init leftovers may be removed, but initialization never invokes this path
on an authored Git checkout. Gateway-capable first boot can initialize locally
because its gateway need not be serving yet. `# ambient: B2` still identifies
the bootstrap access reader for the exact caller inventory in
`base/cluster/machine/tests/test_ambient_callers.py`; refresh does not migrate
that separate dependency.

## Bounded I/O and status

```meiva-contract
id = "git-command-bound"
kind = "invariant"
statement = "run_git passes the per-command timeout to run_bounded, raises CalledProcessError for nonzero exits, and propagates TimeoutExpired. It returns stripped stdout only on success."
code = ["base/deploy/git/memory_repo.py:run_git"]
tests = ["base/deploy/git/tests/test_memory_repo.py::test_run_git_is_bounded_and_kills_the_tree_on_expiry", "base/deploy/git/tests/test_memory_repo.py::test_run_git_raises_on_non_zero_exit", "base/deploy/git/tests/test_memory_repo.py::test_run_git_propagates_the_timeout"]
```

`run_bounded` owns descendant cleanup; `subprocess.run(timeout=...)` alone
would only terminate its direct child. Bounding Git also bounds a refresh
worker that otherwise holds executor shutdown open. The existing bound is per
command, not one deadline for an entire fetch/merge sequence.

```meiva-contract
id = "pool-download-bound"
kind = "invariant"
statement = "Pool downloads enforce both declared and streamed size limits and report transport failures as MemoryPoolBootstrapFailed, preventing an unbounded response from filling memory before installation."
code = ["base/deploy/git/memory_repo.py:_stream_pool", "base/deploy/git/memory_repo.py:download_pool_snapshot"]
tests = ["base/deploy/git/tests/test_memory_repo_pool_snapshot.py::test_download_pool_snapshot_streams_and_enforces_cap_midstream", "base/deploy/git/tests/test_memory_repo_pool_snapshot.py::test_download_pool_snapshot_declared_oversize_rejected_before_body", "base/deploy/git/tests/test_memory_repo_pool_snapshot.py::test_download_pool_snapshot_transport_error_wrapped"]
```

```meiva-contract
id = "repository-status"
kind = "invariant"
statement = "Repository status renders FETCH_HEAD's modification time in the cluster time zone. The existing RepoStatus value and MemoryRepoUninitialized error remain the status surface; inspecting status does not initialize a repository."
code = ["base/deploy/git/memory_repo.py:status", "base/deploy/git/memory_repo.py:RepoStatus", "base/deploy/git/memory_repo.py:MemoryRepoUninitialized"]
tests = ["base/deploy/git/tests/test_memory_repo_bootstrap.py::test_status_last_fetch_renders_cluster_zone"]
```

`RepoStatus.is_clean` covers the working tree and index. The local commit count
is the entire HEAD ancestry; ahead/behind compare to the configured track
branch, despite the historical field names ending in `_main`. Without that
remote reference they remain zero. `last_fetch` renders FETCH_HEAD's mtime in
the cluster time zone, or `never`; an uninitialized repository raises
`MemoryRepoUninitialized` rather than reporting an empty repository.

Parent: [[base/deploy/docs/deploy.meiva.okf.md]].
