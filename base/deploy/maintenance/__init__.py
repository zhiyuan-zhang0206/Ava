"""Maintenance holds on a unit: admission, pause ownership, restart cohorts.

``pause_owner`` is the host-local pause/resume capability journal and ``state``
the typed hold it carries; ``admission`` admits work and tracks
exact-generation progress while a unit is held; ``hold_driver`` names the
shepherd a hold is bound to; ``cohort`` is the durable restart cohort of an
admitted hosted runner; ``cold`` recognizes completed legacy work once its
consumers have exited.
"""
