---
type: doc
title: "Opt-in Unit Capabilities"
description: "Consumer-local homes, identity, generation ledgers, gateway clients, model owners, serving doubles, logging and retry observations."
tags:
- evaluation
- quality-assurance
---

# Opt-in Unit Capabilities

The root plugin roster does not load unit fixtures. Consumer-local conftests
import `unit.homes` (private home and workspace), `unit.identity` (runner and
machine identity), and `unit.generation` (private ledgers and served homes).
Descendant tests inherit those fixtures from their nearest shared test directory.
Directories already at their entry budget use the same existing scope reader
for a directory-level capability declaration instead of adding a conftest.
Indirect consumers count too: `config_authority` needs `unit_home`, and scoped
CLI and PTY fixtures keep their home dependency. `units` retains explicitly
imported agent allocation and environment-file helpers without loading the
assembled gateway.

Model consumers import the immutable catalog fixtures from `model_catalog`.
`unit.config_authority` owns the lightweight configuration authority and its
private-home dependency; `unit.model_owner` owns the explicit SDK installation
and opt-in process binding. A test that only needs authority therefore does
not import Installation or construct a model catalog. The fixture bodies and
their dependencies remain unchanged; consumers choose when to install the SDK
owner. Catalog mutation helpers keep their existing public type aliases.

Logging consumers import `log_capture.loguru_records`; retry consumers import
`retry_waits.retry_waits`, which records the retry library's bounded waits
instead of sleeping. It is never a global no-op wait: wall-clock loops must
still progress, and the existing wait-count bound remains enforced. Serving
lifecycle consumers import their package's `serving_root` observation double.
Those capabilities use local conftests at shared consumer test directories.
When a directory is full, its existing scope declaration names exact consumer
files, including child files when the full child has no declaration of its own.

`gateway_unit` and `sdk_via_gateway` live in `tests/fixtures/unit/gateway.py`.
Their four consumer directories use file-level declarations in
`path_scopes.toml`; putting the assembled app in a shared conftest there would
make unrelated tests depend on every gateway router. These declarations use the
existing pytest/CI scope reader. They add no runtime binding mechanism.

Global environment boot, native DB/Redis provisioning, identity and plugin
restoration, leak checks and host guards retain their original registration
order and behavior. Moving an opt-in unit fixture does not weaken isolation.
