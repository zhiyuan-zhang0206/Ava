---
type: doc
title: Package primitives
description: Documentation parsing, package installation and plugin configuration.
tags: [base]
---

# Package primitives

`base/packages/` owns documentation parsing, package installation and plugin configuration.
Its component nodes describe the current contracts and implementation.

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

## Documented components

- [[base/packages/extensions/docs/install_registry.ava.okf.md]] — Install Registry (`installed.json`).
- [[base/packages/extensions/docs/write-path.ava.okf.md]] — Install Registry Write Path.
- [[base/packages/plugins/docs/enable_config.ava.okf.md]] — Plugin Enable Config.
- [[base/packages/plugins/docs/plugin-config.ava.okf.md]] — Plugin Config Images and Agent Views.
