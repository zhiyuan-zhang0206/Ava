"""Durable deploy state: the cluster deploy lease and host deploy posture.

``cluster_lock`` is the cluster-wide "a deploy owns this cluster" lease;
``host_deploy_state`` the per-host deploy posture and updater lease.
"""
