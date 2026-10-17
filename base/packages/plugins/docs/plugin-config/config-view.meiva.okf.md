---
type: doc
title: Plugin config view — cluster projection gate and pin routing
description: The with_cluster_policy fetch gate and the read-side agent pin routing of base/packages/plugins/config_view.py.
tags: [base, plugins]
---

# Plugin config view — cluster projection gate and pin routing

## Cluster projection and the fetch gate

`with_cluster_policy` composes the local image with the gateway's declared
cluster projection; only a unit that is neither the local config source nor
exempt from fetching expects a packet. A gateway-capable unit that owns its
local image composes from it instead of raising (task #5234).

```meiva-contract
id = "cluster-projection-gate"
kind = "invariant"
statement = """A missing plugin cluster projection raises InvalidConfigData only when the plugin declares cluster fields and the unit expects a fetch: not config_source_is_local() and should_fetch_from_gateway(). A unit whose config source is local composes from its own authority image - for the gateway-capable unit that owns its image, a missing packet is its normal state (task #5234). A present projection must equal the declared cluster fields exactly (SchemaDriftError) and the composed model must validate (InvalidConfigData); a local-source unit keeps its own values."""
code = ["base/packages/plugins/config_view.py:with_cluster_policy"]
tests = ["meiva_builtins/plugins/meiva_fleet/tests/test_owned_configuration.py::test_gateway_unit_without_a_packet_composes_from_its_local_image", "meiva_builtins/plugins/meiva_fleet/tests/test_owned_configuration.py::test_remote_cluster_projection_never_overrides_local_host_gate", "meiva_builtins/plugins/meiva_fleet/tests/test_owned_configuration.py::test_invalid_present_projection_is_rejected_even_on_gateway", "meiva_builtins/plugins/meiva_fleet/tests/test_owned_configuration.py::test_gateway_truth_is_authoritative_and_projection_has_exact_scope"]
```

## Agent pin routing

`resolve_agent_plugin_pins` is the read side of overlay admission; the write
side is `resolve_overlay_targets`.

```meiva-contract
id = "plugin-pin-routing"
kind = "invariant"
statement = """An agent's flat overlay routes each key to its single owning plugin; framework-scope keys stay with base/config/agent_pins.py. A key whose owner is unknown or ambiguous is dropped rather than raised, matching the framework pins reader's drift tolerance: the stored map was validated against a schema that may no longer exist, and a read must not be stricter than the boot that would consume it. Absent and empty overlays resolve to no pins."""
code = ["base/packages/plugins/config_view.py:resolve_agent_plugin_pins"]
tests = ["base/packages/plugins/tests/test_plugin_config_view.py::TestRouting::test_flat_keys_route_to_their_owner", "base/packages/plugins/tests/test_plugin_config_view.py::TestRouting::test_framework_keys_are_not_plugin_pins", "base/packages/plugins/tests/test_plugin_config_view.py::TestRouting::test_unknown_key_is_dropped_not_raised", "base/packages/plugins/tests/test_plugin_config_view.py::TestRouting::test_ambiguous_key_is_dropped", "base/packages/plugins/tests/test_plugin_config_view.py::TestRouting::test_empty_overlay_resolves_empty"]
```
