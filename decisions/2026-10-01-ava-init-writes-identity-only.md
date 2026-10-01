# `ava init` writes a home's identity and nothing else

## Context

`ava start` took 15 flags. Eleven of them describe the machine and exist only to
birth a home: the machine name, three capability flags, the description, the memory
remote, the gateway URL, the reachable host, the CA bundle, the config file and the
runner's capability bundle. The other four select services. Every start, not only the
first, ran the birth inputs through the same comparison code: a repeated flag had to
equal the recorded value, a repeated config file had to match its recorded digest, a
runner re-joined its gateway before anything else. None of that protected anything on
an initialized home, and every one of those branches had to be read and kept correct
by anyone who changed start.

The identity half of start was already a settings-free phase of its own (it wrote the
intent and `.env` before Settings loaded), and the data-plane birth already followed
the intent's phase, not the command. The
[one cluster per host](2026-09-30-one-cluster-per-host.md) plan names the split as its
slice 5.

## Decision

`ava init` takes the eleven birth inputs, publishes the home's intent and `.env`, and
stops. It imports no Settings, starts no process and creates no database; the first
`ava start` provisions the data plane and launches, exactly as the first start does
today. `ava start` takes only the service selection and admits a home only when it is
initialized.

- **Init runs once.** A home that is `configured`, `provisioned` or `ready` refuses a
  second `ava init` without changing a file; the identity of a home changes only by
  `ava cluster destroy` and a new init. An interrupted claim resumes with `ava init`
  and no flags, from the payload the intent recorded, and a flag given then is an
  error. This deletes the comparison of repeated flags with persisted values, and the
  config-digest comparison.
- **Start refuses what init has not finished,** naming `ava init`: a home with no
  intent, a claim still in progress, a detached home. The checks start already made of
  an existing home (the identity keys its roles need, a declared `AVA_SERVICE_PATH`, no
  human secret on a remote unit) are kept, and a capability set that differs from the
  intent's refuses.
- **The runner join moves with the bundle.** `--db-capability` was not only a first
  start input: a runner installs a newer bundle the same way after a write-generation
  rotation. The first join is `ava init --db-capability`; a later bundle goes to
  `ava cluster db-authority install-unit BUNDLE`, beside `issue-unit`, and the rotation
  procedure becomes stop, install-unit, start. Both call one function
  (`cli/unit_join.py`). Start makes no join: a started runner fetches its configuration
  through Settings and probes its gateway.
- **The intent does not change.** Its keys, its phases (`ready` included) and the
  recorded digest stay, so an initialized home needs no edit.
- **The quickstart's secret form is removed.** It exported `AVA_CLUSTER_SECRET` before
  the first start, but birth never read the variable (nor does `--config-file`, which
  refuses derived keys), so the cluster was born with an empty secret. A single box
  runs with an empty secret bound to loopback; a gateway that serves other machines
  mints one at init; a running cluster takes a new one through
  `scripts/data_plane_ops/rotate_cluster_secret.py`. No credential input is added.

## Alternatives rejected

- **Init also provisions the data plane and leaves it running.** It would move the
  failures of initdb, migrations and the first write generation into init, but it
  needs Settings, a converge subset (a converge with the default roster builds
  things a narrowed start would exclude) and a resting state, a running database with
  no application, that stop does not treat as unstarted (only `configured` takes that
  shortcut). Stopping the data plane at the end of init would double the cold start.
- **Init also starts the services.** It brings the four service-selection flags back
  into init, or fixes the roster, and leaves two commands able to finish a first
  start.
- **No new command: start refuses the birth flags on an initialized home.** It saves
  the parser split but keeps one command with two modes and the branches that tell
  them apart.
- **Keep `--db-capability` on start.** It keeps the join, the transport key and a
  network dial in every start for a flag used once per rotation.
- **Init accepts an initialized home when its flags agree.** It brings the comparison
  code back for the convenience of rerunning a script; a script that failed after init
  reruns `ava start`.

## Consequences

- The first `ava start` after init is as slow as today's first start: storage,
  provisioning, migrations and the first write generation happen there. A home that
  stops at `configured` has nothing native to clean up, and `ava stop` on a configured
  gateway home already takes the unstarted shortcut. The home's `.env` can be edited between init and the
  first start, so provider keys need no restart.
- A runner home that predates the intent journal is still admitted by `ava start` on
  what its `.env` and `machine_*` files declare, and the `machine_*` files are still
  read for identity fields and capability flags. `ava init` refuses such a home.
  Retiring both channels needs each such home to be given an intent and its identity
  in `.env`; that is left to its own change.
- Documentation and error messages that told an operator to pass a birth flag to
  `ava start` now name `ava init` or `install-unit`; the doc-reference lint keeps
  them honest.

Follow-up: [a home's identity lives in its `.env`](2026-10-01-retire-machine-identity-files.md)
retires the `machine_*` files and the admission of a home with no intent that the
second consequence above leaves in place.
