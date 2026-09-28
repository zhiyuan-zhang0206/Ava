---
type: doc
title: Locked Python Installation
description: Dependency-free package acquisition; canonical runtime pins and host-local artifact transport, with no cluster initialization effects.
tags:
- cli
- infra
---

# Locked Python Installation

`cli/python_install.py` is a stdlib-only module runnable before project packages
exist. Invoke it with Python 3.12 and an explicit checkout; worktree setup uses
its already-created venv. It neither creates cluster identity nor starts or
provisions services. The script explicitly locates
its checkout, including when `PYTHONSAFEPATH` disables implicit cwd imports.

`cli/_python_index.py` reads one host index from uv settings or the pip settings
uv does not consume. The installer can pass the existing unit `mirror.env`; real
environment values win. `shared/dotenv_boot.py` preserves the same precedence
across both uv single-index aliases when a native command loads the unit files
before calling the installer. Additional indexes remain separate and are rejected
by the installer. These files are never rewritten by discovery. Operators configure package-manager transport independently of cluster start.
Additional/explicit-only indexes fail rather than silently losing their source policy.

The lock-source lint runs before installation. Its stdlib implementation lives in
`shared/python_lock.py`, included in the runtime wheel; the checkout-only
`scripts/lint_python_lock.py` is a thin CLI entry point. Both transports first run
offline, freshness-checked uv export to temporary hashed requirements, before
any uv command can create or recreate the target environment. This also protects
an existing environment whose managed interpreter now differs from the patch
version recorded in `pyvenv.cfg`: native sync may recreate it before checking
lock freshness. PyPI then uses native locked, inexact sync; a mirror installs the
complete exported graph with `uv pip install --no-deps
--require-hashes`, then builds the real project editable with no dependency
resolution. uv evaluates groups and markers; hashes prevent alternate mirror
artifacts, and the real checkout remains the editable target. Updates exclude
new dev installs but retain existing dev packages. Failure aborts the remaining
steps; neither branch mutates `uv.lock` or commits a derived requirements file.
This preflight preserves the environment on a stale lock; it is not an atomic
rollback of later network, build, or installation failures.

Configuration precedence, limits, and first-rollout cautions:
[Machine Python indexes](../conventions/dev-setup.md#machine-python-indexes).
