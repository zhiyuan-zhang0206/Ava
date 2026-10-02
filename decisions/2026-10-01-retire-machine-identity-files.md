# A home's identity lives in its `.env` and nowhere else

## Context

[`ava init`](2026-10-01-ava-init-writes-identity-only.md) records a home's identity
once: its intent carries the capability set, and its `.env` carries the machine name,
the reachable host, the description, the three `AVA_MACHINE_SERVE_*` flags, the memory
remote and the gateway URL. Two older channels outlived that change, because retiring
them needs a hand step on every home that still uses them:

- **Identity files.** `$AVA_HOME/machine_name`, `machine_host`, `machine_description`,
  `machine_serve_{gateway,agent_runner,observability_station}`, `memory_remote` and
  `gateway_url` were read as a fallback behind the matching `.env` key, by seven
  resolvers (`base/cluster/machine.py`, `base/config/data_plane.py`,
  `base/host/env/bootstrap.py`, `base/deploy/git/memory_repo.py`,
  `cli/commands/management/config.py`, `cli/commands/_setup.py`,
  `cli/start_identity.py`) and by the memory plugin's pool script. The resolvers did
  not agree: the settings path took env first, the config command took the file first,
  and the bootstrap probe parsed a typo as off where the settings path raised.
- **Admission without an intent.** `ava start` admitted a runner home that had no
  `start-intent.json` on what its `.env` and the files declared. Such a home could not
  be reached by anything else: `ava init` refuses a home whose `.env` already names an
  identity, and `ava cluster destroy` refuses a home with no intent.

Both contradict the rule `ava start` states about itself: the home's `.env` is the only
source of its identity (`_enter_home` drops every inherited identity key). A file that
disagrees with `.env`, or that holds the only copy of a value, makes a home answer
differently depending on which resolver asks. One home also carried a `machine_role`
file that nothing has read since the capability flags replaced the single role; its
value contradicted the flags and the intent.

## Decision

Delete both channels without a compatibility layer.

- The resolvers read settings (the home's `.env`) and nothing else: an empty value is
  empty, with the same fallbacks as before (`localhost` for the host, none for the
  description, an error for the name and the capability set). The bootstrap probe reads
  the process environment, then the `.env`. The config command reads the `.env` alias.
- `stored_values` returns the `.env` only, so `ava init` and
  `ava cluster db-authority install-unit` see what a start sees.
- `ava start` admits a home only through its intent. A home with no intent is refused
  the way `ava init` refuses it; there is no admission without one, and such a home is
  not supported.
- The memory plugin's pool script reads `AVA_MACHINE_NAME` from the process
  environment, then from the home's `.env` without building Settings, and refuses when
  neither names the machine. It no longer returns a made-up `unknown`, which would have
  pushed the steward's work to a branch named for no machine.

## Alternatives rejected

- **Keep the files as a read fallback.** It keeps two sources that can disagree, keeps
  seven resolvers each with its own precedence, and keeps a stale file looking
  authoritative. It protects only homes that predate `ava init`, which is the set of
  homes this change moves.
- **Refuse a home that still carries the files.** A guard in `ava start` would turn a
  missed hand step into a named refusal. It is protection code for a state that exists
  once, per home, during one rollout, which an operator watches; it would have to be
  removed afterwards, and it would still let every other resolver see no file. The
  rollout checks the state instead (below).
- **Ship the migration as repository code.** The same reason: a one-time conversion of
  old state is run by hand, not carried in the tree.
- **Make the intent the source of the capability flags.** The intent already records
  the roles, and start refuses a `.env` that disagrees with it. Dropping the flags from
  `.env` would also drop them from the bootstrap probe, which runs before Settings and
  before the intent can be read. Not now.

## Consequences

- **A hand step per home that still has the files**, before the first start of the code
  that contains this change. Each file's value becomes the matching `.env` key (`AVA_`
  plus the upper-cased file name: `machine_host` is `AVA_MACHINE_HOST`, `memory_remote`
  is `AVA_MEMORY_REMOTE`, `gateway_url` is `AVA_GATEWAY_URL`). Write through the audited
  `.env` writer (`upsert_env` with an audit site), because an armed home reports an
  unaudited edit as `env_unauthorized_write`. Where both exist and differ, stop: the
  `.env` value is the one every start has used, and the difference is for a person to
  settle. After the keys are in, the capability flags must give the roles the intent
  recorded, and the files are deleted. A `machine_role` file is deleted without being
  read. The step is idempotent.
- **Two orderings are available.** The `.env` half can be done at any time on a running
  home: the old code already prefers the `.env`, so adding a key with the file's value
  changes nothing, and nothing restarts. The file deletions then go in the stop window,
  between stopping the units and starting them, before any command of the new code
  (the [runbook rule](../conventions/runbook.md#updating-a-networked-cluster-in-source-mode)
  for a manual step that rewrites a state file). Or both halves go in the window.
- **A missed step shows at the first start.** A host that was only in a file resolves to
  `localhost`, and a runner start is refused at registration by the loopback guard. A
  description that was only in a file is silently empty. A memory remote that was only
  in a file raises `MemoryRemoteMissing` on the first remote-backed memory operation.
  A missing name raises `MachineNameMissing`. To check a home before starting it: every
  key above that the home declared has exactly one line in its `.env`, and
  `ls $AVA_HOME | grep -E '^(machine_|gateway_url$|memory_remote$)'` prints nothing.
- A home that predates the intent journal needs a hand-written intent or a fresh home;
  no command converts it.
- The skill that tells an agent to resolve its memory branch no longer reads a file; it
  calls the pool script's resolver.
