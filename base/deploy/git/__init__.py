"""Git provenance for deployment: the checkout a unit runs, its commit, and git plumbing.

``gitenv`` (the non-interactive environment every Ava git call runs under),
``github_pr`` (PR capability on the memory repo), ``repo_change`` (frontend /
backend change classification), ``worktree_guard`` (the live-anchor scan before
``git worktree remove``), ``cluster_drift`` (prod-source HEAD and branch drift),
``source_tree_guard`` (source-tree integrity of a source-run home),
``host_version`` (the version derived from the checkout's commit),
``running_sha`` (the commit the gateway last started on) and ``memory_repo``
(memory-pool git operations).
"""
