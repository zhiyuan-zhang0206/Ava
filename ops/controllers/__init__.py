"""Ops controllers — one reconciler per state dimension.

Each module here owns a single dimension of a host's desired state (schema
version, cluster pin, cluster pause) and exposes a ``Controller`` the
``ops.manager.ControllerManager`` runs every tick: observe actual state → diff
against Spec → act to converge, with its own cooldown/backoff. Extracted from the
watchdog daemon's inline reconcile gates so the gate logic has one home and the
watchdog slims to a thin main over the manager.
"""
