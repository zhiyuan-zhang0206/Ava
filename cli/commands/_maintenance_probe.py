"""Compatibility imports for the shared local pause probes."""

from ops.agent_pause_probe import HostIdentity, host_identity, host_identity_or_none, ops_quiescent

__all__ = ["HostIdentity", "host_identity", "host_identity_or_none", "ops_quiescent"]
