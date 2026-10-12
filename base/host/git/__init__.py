"""Git plumbing every Ava git call shares, and the version derived from a checkout.

``gitenv`` (the non-interactive environment every Ava-initiated `git` call runs under)
and ``host_version`` (the date-axis host version derived from the checkout's commit, the
version grammar the plugin gates compare, and the `pyproject.toml` fallback). The deployment
side of git (PR capability, drift, source-tree guards) stays in `base.deploy.git`.
"""
