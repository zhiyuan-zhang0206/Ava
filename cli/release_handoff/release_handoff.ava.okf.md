---
type: doc
title: Image-exec handoff
description: The only contract that crosses release versions (frozen v1) — the previous image verifies a prepared image and runs that image's fixed entry point, from the CLI and as the release_image_exec ops kind.
tags: [cluster-lifecycle, release]
---

# Image-exec handoff

A release is always run by the code of the image it installs, never by the
image it replaces. The previous image only verifies a prepared image in its
home's store and runs a fixed entry point of that image; everything after that
is candidate code. This handoff is therefore the one piece every image must
keep compatible, and it is frozen as `v1`: changing the envelope, the entry
names, the entry argv or the ops wire models takes a two-step release (ship a
reader of the new form first, use it one release later).

## Envelope

The contract lives in the settings-free `shared/api_contracts/release_handoff.py`
(envelope reader, exec argv, wire models), so the ops server can serve it too.

Every request document carries `version` (the JSON integer `1`), `kind`,
`id` (a canonical lowercase UUID string), `home` (absolute, canonical),
`machine` and `executor`, the image that runs the request on this host
(`artifact_digest`, `manifest_digest`, `schema_digest`, `source_commit`). Each
is read in its exact JSON type: a reader frozen into an image can never be
tightened, so `true`, `1.0` or `"1"` for the version and any other UUID
spelling are refused now. The reader ignores every other field and every unknown kind,
so a newer candidate may add request kinds (a fleet request) and fields without
a second release; for the same reason a field meant to constrain what the
previous image does must bump `version`, since v1 ignores it. The executor is verified with the reader's own image
verification: full inventory hashes, packaged source identity, and the image's
ABI tag against this host as observed now. The retained-image store layout and
manifest format are consequently part of the same contract.

## Exec contract

The executor's verified interpreter runs
`-I -B -X utf8 -m cli.release_handoff ENTRY SOURCE` in the image's working
directory, with the caller's environment plus `AVA_HOME=<envelope home>` (an
installed image requires an explicit home). `ENTRY` is one of `receipt`,
`preflight`, `submit`; `SOURCE` is the request path or `-` for the exact bytes
on stdin.

- **CLI** (`handoff.run`): `ava cluster update --prepared REQUEST` reads the
  envelope without loading Settings, refuses a request whose home is not the
  home this CLI resolves (explicit `AVA_HOME`, production source, or the
  checkout's `.ava_home`), verifies the executor, takes its own database
  authority and replaces its own process with the executor's `submit` entry
  (POSIX `execve`; Windows refuses).
- **Database authority across the exec.** The executor image is not the
  home's selected image until its operation selects it, so its boot pass
  admits it to no write generation, yet its submission reads the registered
  units. The CLI, running the home's admitted runtime, takes the login its
  skipped boot pass would have delivered (`shared.host.env.dotenv_boot.operator_db_delivery`):
  the active gateway login and its generation marker (`AVA_DB_URL`,
  `AVA_DB_GENERATION`), without the gateway API token. It travels only in the
  exec environment (never argv, a file or a log); the executor's boot pass
  keeps a delivery naming its home's endpoint. A CLI with no authority to hand
  over (a stale image, a launcher-started process without a delivery) refuses
  before the exec; a home without a ledger adds nothing. This is the caller's
  environment of the v1 contract, so nothing in it changed: an executor that
  predates the delivery keeps or overrides the variables through its own boot
  pass, and a previous image that predates it hands over none, which the
  executor refuses by name before creating an operation. The finite executor
  the submission launches receives none of this: its launch environment is
  fixed, and it dials as the OS-user administrator
  ([[cli/release_transition/write-generations.ava.okf.md]]).
- **Ops** (`release_image_exec`, `ops/cluster.py`): `{entry, image,
  request}` with the request base64-encoded. The unit's ops server requires
  the envelope to name its own home and machine and `image` as executor,
  verifies it in its own store, runs the entry on stdin with a 120 s bound (the
  child is killed on expiry) and returns `{entry, result}`: the entry's one
  JSON object. A nonzero exit, non-JSON output or a timeout is a failed op. The
  op changes nothing else; the entry owns its own journaling.

## Candidate entry (`__main__.py`)

The entry runs only as the image the request names as executor (its code root
must lie in `home/releases/<executor artifact>`), so the code that acts is the
code the previous image verified even if the request file changed after the
check. `submit` is the home release journal's submission
([[cli/release_transition/release_transition.ava.okf.md]]). `receipt` and
`preflight` are a unit's read-only answers to a fleet coordinator
(`cli/release_fleet/entries.py`, [[cli/release_fleet/release_fleet.ava.okf.md]]):
the unit's facts for including it, and whether its own unit request is
admissible now. The previous image needed nothing new for them.

## Boundaries

- The coordinator that calls the ops kind, the fleet request kinds and the
  unit follower belong to the fleet release transition
  ([[cli/release_fleet/release_fleet.ava.okf.md]]). The ops kind needs the
  unit's candidate image reference up front; it does not look images up by
  commit.
- The coordinator's own listener and its per-unit authentication (the
  enrollment secret) are separate: see
  [[shared/cluster/authority/wiring.ava.okf.md]].
