---
type: doc
title: "Opt-in Unit Capabilities"
description: "Consumer-local unit homes, machine identity, private generation ledgers and assembled gateway clients."
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

`gateway_unit` and `sdk_via_gateway` live in `tests/fixtures/unit/gateway.py`.
Their four consumer directories use file-level declarations in
`path_scopes.toml`; putting the assembled app in a shared conftest there would
make unrelated tests depend on every gateway router. These declarations use the
existing pytest/CI scope reader. They add no runtime binding mechanism.

Global environment boot, native DB/Redis provisioning, identity and plugin
restoration, leak checks and host guards retain their original registration
order and behavior. Moving an opt-in unit fixture does not weaken isolation.
