---
type: doc
title: Plugin config images and agent views
description: Explicit ownership, boot overlays and validation of plugin configuration.
tags: [base, plugins]
---

# Plugin config images and agent views

Ownership follows the
[plugin config image decision](../../../../../docs/decisions/extensions/plugins/2026-10-09-plugin-config-images-have-an-owner.md).

The config face declares one frozen Pydantic model. Its full authority image
lives at `$MEIVA_HOME/configs/<plugin>/config.json`. Reading a missing image can
use declared defaults; binding creates that initial image. Existing malformed
data and schema drift raise their explicit errors, without repair or fallback.
`meiva plugins update` owns schema reconciliation.

The SDK installer binds instances into its local build and publishes them on
`Installation.configs`. The declared class is the instance's type; there is no
second registry to synchronize. Rollback removes local bindings and a failed
installation publishes nothing. The SDK module reads its current installation;
services and agent views receive the resolved image explicitly.

The exec child applies core overlay fields before loading the SDK and plugin
overlay fields after installation. The latter builds a replacement config
image and the installer publishes it. Frozen instances and the caller's input
mapping are not mutated. This does not introduce in-process plugin reload.

The agent host holds its boot image and supplies it when resolving
`AgentSlices`. `PluginConfigView` layers only that agent's pins over the image,
memoizing frozen instances within that view. External attachments construct
the same view from their process's installation. Separate roots and agents
do not share a registration table or overlay state.

Plugin config has no `birth_config`: without an explicit agent override, a
new process reads the current authority image. Core frozen/live field rules,
fresh-file config-service reads, outbox ticks, and bootstrap delivery retain
their own existing update boundaries. Provider credentials remain in the
declared secret-delivery channel, outside plugin config images.

Gateway and ops need no SDK installation to validate an overlay. They read
enabled config declarations; an SDK caller additionally supplies its bound
image so validation retains its existing base values. Unknown keys, ambiguous
owners, non-overridable fields and invalid values are rejected before writes.

`config_registration.plugin_bootstrap_config` builds the gateway cluster-policy packet
from discovered declarations and fresh authority disk images. It includes only
declared `cluster-pinned` and `cluster-default` fields, rejects sensitive fields,
and retains strict schema admission. The framework config reader receives that
packet explicitly and does not discover plugins or own their authority images.

## Schema reconciliation

```meiva-contract
id = "schema-reconciliation"
kind = "invariant"
statement = """`meiva plugins update` merges the disk image to the declared schema: new fields take their class defaults, removed fields are dropped so the strict equality check in the disk reader keeps passing — a stale field would make every spawn raise SchemaDriftError — and an incompatible type change raises InvalidConfigData for manual migration. An aligned image is a no-op; a missing image receives the defaults."""
code = ["base/packages/plugins/config_registration.py:merge_disk_image_schema", "base/packages/plugins/config_registration.py:_instantiate_from_disk"]
tests = ["base/packages/tests/plugin_config/test_plugin_config.py::test_merge_disk_image_adds_new_field", "base/packages/tests/plugin_config/test_plugin_config.py::test_merge_disk_image_drops_removed_field", "base/packages/tests/plugin_config/test_plugin_config.py::test_merge_then_bind_resolves_removed_field_drift"]
```

## Overlay admission

```meiva-contract
id = "overlay-admission"
kind = "invariant"
statement = """SDK-side overlay validation splices the overlay into a FRESH declaring sub-model built from env + defaults — never the validating process's own settings, whose profile may hold only some domains (reading the old settings was the #4423 boundary 500) — then runs the framework range validators; plugin fields validate against the declared class. Success validates only: settings and the supplied image are not modified. A flat key must uniquely match a framework field or a plugin Config field, and only per_agent fields are overridable; violations raise InvalidConfigOverlay naming the key and reason."""
code = ["base/packages/plugins/config_registration.py:validate_config_overlay_shape", "base/packages/plugins/config_registration.py:resolve_overlay_targets"]
tests = ["base/packages/tests/plugin_config/test_plugin_config.py::test_validate_config_overlay_uses_declaring_model_in_gateway_profile", "base/packages/tests/plugin_config/test_plugin_config.py::test_validate_config_overlay_runs_declaring_model_validators_in_gateway_profile", "base/packages/tests/plugin_config/test_plugin_config.py::test_resolve_overlay_targets_non_per_agent_raises"]
```

## Framework range validators

```meiva-contract
id = "framework-range-validators"
kind = "invariant"
statement = """The complete per-agent overlay surface with a value universe is range-checked: model names against the catalog (an unregistered id would crash the next boot at model build — #1704), durations / timeouts strictly positive and finite (a NaN never fires, an Inf defeats a cap), probabilities in [0, 1], fractions in (0, 1], counts non-negative; None passes (the unset sentinel) and non-numerics fall through to Pydantic. Plugin config fields are deliberately not range-checked — their schemas own their semantics."""
code = ["base/packages/plugins/config_registration.py:_FRAMEWORK_RANGE_VALIDATORS", "base/packages/plugins/config_registration.py:_range_validator", "base/packages/plugins/config_registration.py:_validate_model_membership"]
tests = ["base/packages/tests/plugin_config/test_plugin_config.py::test_validate_config_overlay_unknown_llm_model_raises", "base/packages/tests/plugin_config/test_plugin_config.py::test_validate_config_overlay_out_of_range_rejected", "base/packages/tests/plugin_config/test_plugin_config.py::test_validate_config_overlay_boundary_values_accepted"]
```

## Service birth snapshot

```meiva-contract
id = "service-birth-snapshot"
kind = "invariant"
statement = """A launched service validates its birth snapshot: a present packet must name this plugin and contain its complete declared image; malformed packets never fall back to disk or defaults, and an absent carrier is distinct from an invalid one."""
code = ["base/packages/plugins/config_registration.py:read_service_config", "base/packages/plugins/config_registration.py:service_config_packet"]
tests = ["base/packages/plugins/tests/test_service_config_snapshot.py::test_service_birth_snapshot_survives_authority_image_changes", "base/packages/plugins/tests/test_service_config_snapshot.py::test_present_service_packet_never_falls_back", "base/packages/plugins/tests/test_service_config_snapshot.py::test_absent_service_carrier_is_distinct_from_invalid"]
```

View contracts: [[config-view.meiva.okf.md]]
