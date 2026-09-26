---
type: doc
title: Local release preparation
description: Committed source and explicitly supplied local artifacts produce one inactive image and a source-to-image receipt.
tags:
- release
---

# Local release preparation

`prepare_image(Preparation)` binds `cli.release_build.build_application` to
`shared.runtime_prepare.prepare_release`. It adds the source-to-image evidence
contract; wheel construction and native image assembly remain in those existing
builders. Linux and macOS are the supported preparation platforms.

The command is independent of the runtime CLI dispatcher:

```bash
.venv/bin/python -m cli.release_prepare \
  --repo /absolute/source-repository --commit FULL_COMMIT_SHA \
  --work /absolute/new-evidence-directory --store /private/home/releases \
  --inputs /absolute/local-inputs.json
```

The input JSON is the strict, versioned `LocalInputs` model in `models.py`.
Paths must be absolute, canonical and local. Work must not exist; its parent,
the private release store and build cache must already exist. Source, supplied
inputs, cache and output locations cannot overlap in ways that permit output
writes into source or supplied artifacts. The native builder requires a 0700
store. The adapter does not create a home or infer one from environment settings.

## Supplied-input boundary

| Field | Supplied material and expected identity |
|---|---|
| `python` | Managed Python tree and inventory digest; internal file symlinks are materialized by the native builder |
| `wheelhouse` | Flat dependency-only wheel directory and inventory digest; no supplied Ava wheel |
| `requirements` | Hash-required dependency requirements file and SHA-256 |
| `source_lock_digest` | SHA-256 of the committed archive's `uv.lock` |
| `build_constraints` | Complete hash-required build-tool constraints file and SHA-256 |
| `uv` | Explicit local executable and SHA-256 |
| `cache_dir` | Explicit pre-populated offline build cache, trusted by the caller |
| `frontend`, `collector`, `plugins` | Optional prepared trees and their inventory digests |
| `required_plugins` | Sorted unique external plugin names that must be present in the supplied plugin tree |

File input objects use `path` and `digest`; tree input objects use `root` and
`digest`. The Python inventory follows the managed interpreter copier's in-tree
file-symlink rules. Other inventories use `tree_inventory` and
`inventory_digest` from `shared.runtime_prepare`. Requirements and dependency
wheels are copied into the exclusive work directory and rechecked against the
supplied hashes before assembly. Optional assets use the existing native
builder's layout and dependency checks. Absence of an optional asset makes no
claim that a service requiring it can start.

No packages or build backends are downloaded. The committed application build
runs `uv --offline build --no-config --no-sources --require-hashes` with the
supplied Python, cache and mandatory `--build-constraints` file.
Dependency installation uses the existing native builder's offline,
hash-required wheelhouse path. The cache must already contain the approved
build backend and its dependencies. Missing or incompatible hash-pinned build
requirements refuse; a populated cache does not authorize unpinned resolution.

Source-lock, requirements and wheelhouse digests are separate named provenance
fields. Their presence does **not** prove the supplied dependencies were
resolved from that lock. The caller trusts the supplied artifacts. A locked
acquisition receipt connecting those materials is outside this boundary and is
required before publication can claim that provenance. The maintained producer
of those connected inputs is [[cli/release_prepare/acquisition/acquisition.ava.okf.md]].

## Evidence and failure

`work/request.json` captures the exact request before building. The application
subdirectory retains its Git archive, extracted source, built wheel and build
receipt. Only committed source is built; dirty checkout files are not inputs.
The archive lock digest, embedded wheel identity and build receipt must agree.
Full native image verification then checks every installed member, and the
installed application identity must equal the captured source identity,
including commit, tree, archive, schema and migration names.

Success writes canonical `work/receipt.json` and emits the same JSON to stdout.
The receipt binds the request, source, build input digests, current platform,
artifact digest, manifest digest, schema digest and retained executable/cwd.
It is local preparation evidence, not a publication signature or activation
authorization. Consumers still verify the complete image at admission.

Failure writes `work/failed.json` with the phase and exception. Work and partial
generations remain available for inspection; they are never silently deleted,
repaired or reused. A retry needs fresh work and a store without that partial
generation, after the caller explicitly accounts for the retained evidence.
An initial path refusal or inability to create the work directory can occur
before an evidence directory exists.

## Authority

Preparation does not import Settings in its controller, select a release,
register a cluster, operate services, or connect to a database. Existing native
assembly probes import installed application modules with network connections
denied and disposable scratch-home settings; they do not load the target
home's settings. The target selector and initialized home remain untouched.
Readback detects changed inputs but does not claim a lock against independent
filesystem writers. Activation belongs to
[[cli/release_transition/release_transition.ava.okf.md]].
