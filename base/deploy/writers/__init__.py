"""Managed-writer publication: evidence, barrier, observation and admission.

``publication`` owns the current/pending publication envelope; ``barrier`` the
typed writer evidence and the rollout's transaction fence; ``observation`` the
read-only process/session observations; ``runtime_admission`` the locked
publication admission decision for the loaded runtime.
"""
