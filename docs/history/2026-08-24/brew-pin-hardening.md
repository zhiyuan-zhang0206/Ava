# Homebrew pin hardening

## Context

An interactive `brew upgrade` can upgrade unrelated installed formulae. On
2026-08-23 it moved Redis from 8.8.0 to 8.10.1 and contributed to a
cluster-wide message-bus outage. The host had no Homebrew automation; avoiding
unreviewed dependency upgrades depended entirely on remembering not to run a
broad upgrade command.

## Decision

The macOS data-plane install and runtime resolver target the versioned
`redis@8.2` formula. Provisioning exports Homebrew's no-auto-update and
no-install-upgrade controls before any install path can invoke brew.

The approved pin manifest contains twelve formulae, including both `redis` and
`redis@8.2`: the versioned formula is Ava's runtime, while the installed
unversioned formula must also remain pinned so a broad upgrade cannot move it.
A manifest formula that is not installed on a host is not required to be pinned
there; the check compares against the installed set.
Converge checks the manifest during lifecycle commands, and both watchdog roles
repeat the read-only check at runtime. Drift warns during converge and emits one
ERROR per missing-set episode from the watchdog.

Automatic re-pinning was rejected. Dependency upgrades require explicit manual
approval, so Ava detects and names drift but never mutates package pins. The
operator repairs a finding with `brew pin <formula>` after judging the host.
