---
type: doc
title: Locked Python Installation
description: Dependency-free installer shared by install.sh and the bounded updater; canonical runtime pins, host-local artifact transport.
tags:
- cli
- infra
---

# Locked Python Installation

`cli/python_install.py` is a stdlib-only module runnable before project packages
exist. `scripts/install.sh` selects managed Python and its absolute script path;
worktree bootstrap uses its already-created venv. The script explicitly locates
its checkout, including when `PYTHONSAFEPATH` disables implicit cwd imports.

`cli/commands/_update_uv_sync.py` imports the installer and its stdlib dependency
closure before checkout. It invokes that retained function with an explicit target
repo, so staging and historical rollback do not require the target to contain the
new helper. Each uv step runs through the existing process-tree timeout with one
shared monotonic deadline. Editable write-window protection, failure recovery and
post-install import proof surround the complete sequence.

`cli/_python_index.py` reads one host index from uv settings or the pip settings
uv does not consume. The installer can pass the existing unit `mirror.env`; real
environment values win. These files are never rewritten by discovery. Explicit
mirror profile selection remains the install script's existing persistent action.
Additional/explicit-only indexes fail rather than silently losing their source policy.

The lock-source lint runs before installation. PyPI uses native locked, inexact
sync. A mirror uses offline, freshness-checked uv export to temporary hashed
requirements, installs the complete exported graph with `uv pip install --no-deps
--require-hashes`, then builds the real project editable with no dependency
resolution. uv evaluates groups and markers; hashes prevent alternate mirror
artifacts, and the real checkout remains the editable target. Updates exclude
new dev installs but retain existing dev packages. Failure aborts the remaining
steps; neither branch mutates `uv.lock` or commits a derived requirements file.

Configuration precedence, limits, and first-rollout cautions:
[Machine Python indexes](../conventions/dev-setup.md#machine-python-indexes).
