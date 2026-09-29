"""This machine's substrate: OS integration, process and filesystem primitives, converge.

Sub-packages: ``system`` (the PlatformBackend facade, OS job registrations,
capability probes, native scheduler observation) and ``converge`` (host
converge helpers).

Top-level modules: ``proc`` (process liveness, force-kill and bounded
subprocess runs), ``atomic_io`` (same-directory atomic replacement),
``private_storage`` (owner-only storage for secrets and uploads),
``resource_sample`` (one live CPU / memory / disk / battery reading),
``config_validators`` (capability validators for remote host-config writes),
``macos_firewall`` (the Application Firewall allow list) and ``brew_pin``
(operator-approved Homebrew pin warnings).

This door is docstring-only: lightweight members such as ``proc`` stay cheap
to import on their own.
"""
