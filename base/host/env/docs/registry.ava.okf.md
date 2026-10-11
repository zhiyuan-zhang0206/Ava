---
type: doc
title: Environment declaration registry
description: Settings metadata, generated boot index, provider and passthrough declarations, and the explicit classifications consumed by environment projections.
tags:
- configuration
- environment
---

# Environment declaration registry

`base/host/env/registry.py` owns the environment-key inventory and its
boot/child projections. Settings fields declare aliases and metadata at their
class declarations (`json_schema_extra`); `config_registry.py` builds the field
registry without constructing Settings. Before Settings exists, boot-time
projections read the generated static index in `config_lite_table.py`.

Non-Settings passthrough keys include display/home/account variables, network
proxy settings, temporary directories and Windows system keys. PATH and
VIRTUAL_ENV are registered inventory keys rebuilt at the delivery site rather
than copied blindly. Enabled provider-plugin bindings declare removable provider
keys; those declarations load at the delivery boundaries that consume them.

## Consumers

- **Forwarding** (`child_env(role)`) — the parent-to-child env view of a daemon or session child. Native launchers receive an environment dict; secrets never ride argv.
- **Keep/drop** (`env_authority_drop_set(role)` / `env_keep_set(role)`) — dotenv_boot's own-environ surgery, as set-membership queries.

The consumption matrix decides which process reads each key. Capability/scope
metadata validates those choices; it does not infer every consumer. Three
hand-maintained classifications remain in `registry.py`:

- `_HEALTH_PORT_SERVICES` selects services whose declared health-port aliases
  feed the health/derived environment views.
- `_IDENTITY_FIELDS` selects fields whose authority belongs to the child's own
  home rather than the parent's inherited environment.
- `_DERIVED_FIELDS` selects cluster-isolation fields stripped by `derive_env`.

Adding a field declaration therefore does not automatically update every
projection: a new consumption role may need a classification edit. Alias lookups
come from the generated declarations and fail on missing fields. Passthrough
rows that duplicate Settings aliases are rejected.

## Related owners

- [[base/docs/configuration/configuration.ava.okf.md]] — Settings composition and configuration fields.
- [[base/host/env/docs/audit.ava.okf.md]] — environment file writes and audit integrity.
- [[base/host/docs/host.ava.okf.md]] — host primitives.
