---
type: doc
title: Captured release input acquisition
description: Online package acquisition binds a committed source archive to retained distributions, built wheels and optional frontend or collector assets.
tags:
- release
---

# Captured release input acquisition

`acquire.acquire_inputs(Acquisition)` prepares production `LocalInputs` for
`prepare_image`. It receives an exact local repo and commit, a new exclusive
work directory, an explicit approved uv executable and hash-pinned build
constraints. It has no cluster home, release store, selector or outage step.

```bash
.venv/bin/python -m cli.release_prepare.acquire --request /absolute/acquisition.json
```

`acquisition_models.py` defines the strict JSON request and receipt. The CLI
emits the successful receipt to stdout; the work directory retains both
`acquisition-receipt.json` and `local-inputs.json`. A caller passes the latter
through the ordinary inactive image preparation API. `verify_acquisition`
rechecks local evidence without loading runtime settings or selecting an image.

## Source and tool authority

`cli.release_build.capture_source` supplies the same isolated Git archive
mechanism used by application building. Dirty checkout files, moving refs and
ambient Git configuration do not supply code or dependency inputs. The archive,
source identity and original source pins are retained. Lock, project and asset
builder inputs must continue to match their original archive members at the
acquisition boundaries; rehashing a modified working copy cannot admit it.

The captured `.python-version` selects an exact Python 3.12 patch. The approved
uv tool installs it under the private acquisition directory with executable
registration disabled. Its version and native prefix are checked before use.
Python's own bundled `ensurepip` seeds a disposable build environment; the
wrapper never requests an unpinned pip package.

`build-tools.txt` is the reviewed hash-pinned build closure. Its setuptools
version is 84.0.0, matching the application lock. Hatchling, wheel and all their
dependencies are separately pinned in this build-only file. Updating that
file requires an explicit resolution and review; ordinary acquisition never
rewrites it or resolves missing build requirements. The closure can be resolved
with the approved uv using `pip compile --generate-hashes --python-version 3.12`
over hatchling, wheel and `setuptools==84.0.0`. Hash-required wheel downloads and
installation preserve the exact supplied closure. Package builds use the same
constraints and refuse dependencies absent from it.

`acquisition_process.Commands` supplies private HOME, temporary directories,
Python installation and uv/npm caches. Ambient Ava credentials, virtualenvs,
Python import paths, pip indexes and user package-manager configuration are not
forwarded. Every completed or timed-out command retains its output, verdict, UTC
start and monotonic elapsed duration. `command-progress.json` is written before
launch and updated after completion, timeout or interruption, making long package
downloads visible while the original source preview remains available. It is a
diagnostic view, never permission to reap or restart; interrupted commands without
an exit result never acquire a guessed return code. Receipts retain completed
command timing for comparing cold acquisition with verified input reuse.
`shared.posix_command.run_owned_command` gives acquisition, application builds
and offline image preparation one POSIX process group within its caller's
session from launch. A finite outer preparation owner can close that whole
session when a coordinator dies. The existing execution-domain
closer confirms group closure before the direct parent reaps its leader; an
early leader exit cannot erase custody of ordinary reparented children. Success
requires natural closure within the command deadline. Timeout or interruption
closes the owned group with a bounded confirmation; killing unfinished work
never turns the original failure into success. Private file-backed output
avoids an inherited pipe delaying cleanup. This is not a sandbox for build code
that deliberately creates an independent process group or session.
Native compilers and OS
build support remain host prerequisites; acquisition does not install or upgrade
them. The receipt records produced bytes, not reproducible compiler output.

## Locked dependency derivation

The captured project exports `uv.lock` with `uv export --locked --no-dev
--no-emit-project`; the export cannot mutate the lock. The managed interpreter's
pip downloads the exported requirements with hashes required, dependency
resolution disabled and build isolation disabled. Raw downloaded wheels and
source archives are retained separately. Every downloaded artifact must match
a package and artifact hash in the captured lock.

Downloaded wheels are privately copied. Source archives are converted one at
a time with the existing offline `uv build`, mandatory build constraints and
hash checking. Each resulting wheel must have the locked package identity.
The receipt distinguishes `downloaded-wheel` from `built-wheel` and records
the source artifact hash and resulting wheel hash. In particular, a locally
built wheel's hash is not represented as a hash from `uv.lock`. Final runtime
requirements pin the exact produced wheels for offline installation.

This is recorded derivation through the admitted package tools, not a custom
resolver or independent reproducibility attestation. The source archive, lock,
export, build-tool artifacts, per-wheel edges, command logs and final input
inventories remain available for readback.

## Reusing raw distributions

Exporting a prior acquisition's raw distributions into a private store, and
reusing them hash-bound from a fresh acquisition, moved to its own node:
[[reusing-raw-distributions.ava.okf.md]].

## Optional assets

`FrontendTools` declares a Node binary, its bundled npm package tree and the
public gateway port. The Node version must match the captured
`scripts/runtime-node-version`. Both tools are privately copied; npm uses a
private cache and the captured `ui/web/package-lock.json` for `npm ci` and the
existing build script. `scripts/prepare_frontend_release.mjs` supplies the
standalone artifact and its original verification contract. Automatic Node
acquisition is outside this API; no installed system Node is upgraded.

The collector option invokes captured `scripts/prepare_otel_release.py`, which
uses the settings-free `shared.collector_artifact` downloader. Runtime converge
uses that same downloader. Its existing version and platform archive checksums
are the acquisition authority; no second binary builder or runtime command
dispatcher is involved. Missing output directories fail rather than becoming
empty verified inventories.

External plugins remain explicitly supplied, hash-verified prepared trees with
required names. Acquisition does not discover plugins from a running home or
execute scaffold hooks. The preview model fixture is a separate proof input and
is not part of this production acquisition path.

Failures retain `failed.json`, command evidence and partial artifacts. Failed
work is not reused or repaired. Fresh work is required for another attempt.
Work must be disjoint from every supplied source/tool tree, including when an
input directory would otherwise be the direct parent of the new work directory.
These checks observe owned inputs at explicit boundaries; they do not claim a
global filesystem writer lock or an execution sandbox for trusted build code.
Image admission and activation remain in
[[cli/release_prepare/release_prepare.ava.okf.md]] and
[[cli/release_transition/release_transition.ava.okf.md]].
