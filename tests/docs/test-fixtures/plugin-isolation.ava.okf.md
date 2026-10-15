---
type: doc
title: Plugin and startup fixture isolation
description: Restoring installed SDK surfaces and neutralizing inherited host settings.
---

# Plugin and startup fixture isolation

`tests/fixtures/plugin_registrations.py` uninstalls an SDK surface only when the
test began without an installation and ended with one. An already-installed
starting state is left alone. Uninstall removes the installation together in
reverse layer order, with the outermost usage recorder removed before plugin
wraps. Hooks, state and prompt sections are declared contributions, not mutable
process-global registrations that this fixture needs to clear.

`tests/_test_env_file.py` removes inherited LGTM variables before importing
project Settings. Absence has different semantics from writing default values.
It remains an early plain helper outside fixture collection.
