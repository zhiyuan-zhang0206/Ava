---
type: doc
title: Retained plugin runtime roots
description: Prepared external plugin closure and fixed-generation read-only discovery, separate from mutable installation targets.
tags:
- shared
- runtime
---

# Retained plugin runtime roots

Wheel runtime discovery reads external plugins from the loaded generation's
`plugins` directory. `shared.paths.plugins_dir()` remains the mutable per-home
installation/scaffold destination; it is not redirected into an image. Builtin
plugins remain wheel resources. Both agent extension discovery and machine
service discovery consume the same retained external roots. Machine service
presence remains distinct from agent-facing plugin enable state.
Plugin command/MCP configuration reads and the hosted plugin-change fingerprint
also use this read root; changing a future install cannot restart the current
host or redirect delayed discovery to mutable code.

Preparation accepts complete privately hashed package trees with existing v2
manifests and required external names. Missing entry points/manifests, name
conflicts, unknown host capability or MCP executable requirements, dependency mismatches, links,
special files and checkout/secret configuration refuse preparation. Python
dependencies must already be in the locked wheel environment; preparation does
not install new packages to satisfy a plugin. Code, sibling modules, static and
setup resources are included in the same inventory. Manifest-required names
also contribute to candidate identity.

Actual extension/service import verification uses a separate private scratch
unit home outside the generation and blocks socket connections. Its disk config
is discarded afterward. This executes trusted candidate code, not production
mutable plugin code, and is not an adversarial same-UID sandbox. No scaffold is
invoked: arbitrary plugin setup/data migration and external host prerequisites
still need explicit pre-maintenance closure before full service activation.
Legacy source discovery and its fail-soft agent import behavior remain intact.

CI prepares a real wheel with a declared external plugin, imports a sibling and
static resource through the actual agent extension loader with source absent,
discovers its service even when the agent plugin is disabled, and verifies that
poisoning the mutable installation directory does not change loaded code. A
half-installed next input is rejected before a generation write or executable
invocation, then the retained image is launched successfully. This does not
claim Windows, arbitrary optional native dependencies or whole-fleet activation.
