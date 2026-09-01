# Settings-free config CLI repair path

## Decision

`ava config` treats field metadata and current `.env` bytes as a separate
bootstrap surface from the runtime `Settings` singleton. The gateway thin
client resolves its URL and bearer credential directly from process
environment or unit files. `--local` reads the unit `.env` directly, gates the
requested fields by metadata and local scope, validates the complete candidate,
then writes only after validation succeeds.

The `settings` public object remains the normal runtime interface and retains
its normal import-time fail-fast behavior. The existing settings-lite
`AVA_CONFIG_FETCH=skip` mode now defers construction until first attribute
access, permitting the config registry and candidate validator to load declared
Settings models without loading a broken `.env` into the process or constructing
the invalid singleton.

## Rationale

The candidate-validation boundary recorded in
[`config-candidate-validation.md`](config-candidate-validation.md) rejects
cross-field-invalid writes before persistence. Its intended recovery use was
blocked because importing the config CLI built `Settings()` first; an incomplete
OSS restore-proof transition consequently prevented the CLI from reaching the
validator that could repair it. A gateway-only repair was also insufficient
when the gateway was down.

Keeping a second, hand-maintained registry or a separate parser for local
config was rejected because aliases, scope, sensitivity, type, and restart
metadata would drift from the Pydantic field declarations. The existing
registry remains the single metadata source, and candidate validation remains
the single cross-field validation definition.

## Consequences

- `ava config get/set/unset --local` can inspect and repair a unit `.env` while
  runtime Settings construction would fail.
- Sensitive local values stay masked; invalid candidates leave `.env` bytes
  unchanged.
- Cluster-scoped local writes remain limited to a unit that owns cluster config;
  pure runners must use the gateway path.
- Runtime consumers retain the established `settings.<domain>.<field>` API and
  normal import-time fail-fast behavior; settings-lite repair commands build
  only if they later read runtime settings.
