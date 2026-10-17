---
type: doc
title: Process configuration profiles
description: Configuration domains follow process consumers and preserve complete administrative reads.
tags: [configuration]
---

# Process configuration profiles

The constants module can be imported without constructing Settings. The gateway
profile includes its messaging, memory and telemetry daemons; their domains
cannot be derived from the configuration panel's capability labels. Agent hosts
and execution children use the agent profile, while runner support daemons use
the runner profile. The bidirectional consumption guard detects both missing
domains and domains left behind after their last consumer is removed.

Gateway endpoints and agent-published timeline snapshots both consume display
defaults. Agent lifecycle notification and delivery consume daemon settings.
Runner browser MCP consumes sandbox connection timeouts, ops lifecycle consumes
the model setting, and support-daemon event emission consumes observability
settings. The To user read guards (`to_user`) span all three profiles: the ops
listener and the gateway enforce them, and the reading functions sit in the
agent-face closure. These reads explain domains that span more than one process
profile.

An omitted profile argument reads the launcher's environment marker; explicit
`profile=None` constructs the complete model. `PROFILE_UNSET` distinguishes those
cases. Configuration-service reads retain their explicit complete authority for
bootstrap payloads, the configuration panel and overlay snapshots, even when the
process view is restricted. Reflective `get_field` reads still enforce the process
boundary. The profile tests compare all served keys and excluded-domain values;
they do not require unrelated in-profile values to equal the fixture-mutated
singleton. Retired environment keys remain inert so an upgrade does not require
rewriting the unit's environment file before startup.

Restart metadata names the process kind that consumes a setting. Gateway-owned
messaging and memory fields require a gateway restart, which includes gateway
daemons; naming an agent restart or no restart would leave their boot-time reads
unchanged. Registry validation rejects unknown restart labels and process kinds
that lack the field's domain. The profile tests cover those metadata constraints
alongside construction and access behavior.
