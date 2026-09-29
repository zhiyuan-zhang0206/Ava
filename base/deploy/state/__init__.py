"""Durable deploy state: the cluster deploy lease, host deploy posture, legacy pin.

``cluster_lock`` is the cluster-wide "a deploy owns this cluster" lease;
``host_deploy_state`` the per-host deploy posture and updater lease;
``cluster_pin`` the legacy pin row no current lifecycle writes.
"""
