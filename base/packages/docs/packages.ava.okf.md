---
type: doc
title: Package primitives
description: Documentation parsing, package installation and plugin configuration.
tags: [base]
---

# Package primitives

`base/packages/` owns documentation parsing, package installation, plugin configuration
and the shared text-length budget primitive. Its component nodes describe the current
contracts and implementation.

`plugin_config_images.py` owns whole-image validation, revisions and the locked
atomic writer shared by plugin installation and the config admin API. It stays
independent of Settings and admin metadata so first-time plugin installation in
an execution child preserves the boot-lite config boundary.

First-time binding uses the writer's create-only CAS. When another boot creates
the image first, binding reads and validates that winning whole image before
publishing the plugin config. Binding also rereads an image created after its
initial missing-image read. Invalid images, disappearing files and unexpected
read/write errors still fail binding; the default writer never overwrites an
existing authority.

Configuration upgrades reconcile the declared schema in memory before the image is
rewritten; retired fields follow the schema merge's existing removal rule.

`composer_commands.py` owns the composer `/name` commands: where their files live, the
merged and sorted catalog, and how a message expands. It reads no installed skills itself; every
function takes the `Skill` list (`skills/tree.py`) as an argument and `CommandCatalog` binds one
source of it, handed over by the composition root (`ava.skills.names` in the agent host, the
gateway and the ops runner). The agent's claim node receives the catalog through
`AvaContext.commands`.

Expansion preserves the typed command order and adds no chain policy or actor
framing. Explicit command files override a same-named skill command. Catalog
names use dash/colon spelling; matching also accepts legacy underscore/dot
spelling, while the expansion echoes the spelling supplied by the caller.
Only a registered command at a command boundary begins another segment; paths,
mid-sentence names and an unrecognized `/recap/review` token are ordinary text.

## Documented components

- [[base/packages/docs/composer-commands.ava.okf.md]] — Composer command discovery and expansion.
- [[base/packages/docs/text-budget.ava.okf.md]] — Text-length budgets: CJK-aware measurement and the soft/hard verdict.
- [[base/packages/extensions/docs/install_registry/install_registry.ava.okf.md]] — Install Registry (`installed.json`).
- [[base/packages/extensions/docs/write-path.ava.okf.md]] — Install Registry Write Path.
- [[base/packages/plugins/docs/enable_config.ava.okf.md]] — Plugin Enable Config.
- [[base/packages/plugins/docs/plugin-config.ava.okf.md]] — Plugin Config Images and Agent Views.
