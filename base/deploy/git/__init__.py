"""Git provenance for deployment: the checkout a unit runs, its commit, and git plumbing.

``github_pr`` (PR capability on the memory repo), ``repo_change`` (frontend /
backend change classification), ``worktree_guard`` (the live-anchor scan before
``git worktree remove``), ``cluster_drift`` (prod-source HEAD and branch drift),
``source_tree_guard`` (source-tree integrity of a source-run home),
``running_sha`` (the commit the gateway last started on) and ``memory_repo``
(memory-pool git operations).
"""
