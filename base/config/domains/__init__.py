"""The per-domain `BaseSettings` sub-models of the flat field registry.

Each sub-model keeps its fields' env aliases, so `.env` and the wire surfaces are
unchanged; field names stay globally unique across all of them. The registry that
names the domains and builds them is `DOMAIN_MODELS` in
`base/host/env/config_registry.py`; this package only holds the field definitions.
Groups: `agent`, `daemon`, `services`, `channels` (IM adapter credentials),
`storage` (data plane, WAL-G) and `observability`; single-module domains sit beside them."""
