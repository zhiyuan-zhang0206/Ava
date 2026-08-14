# A deregistered home is stopped at the start gate, not disarmed on disk

## Context

`ava cluster destroy` frees a cluster's registry slot and leaves the home's
files — `.env` included — untouched. The port block therefore keeps existing in
two places that can drift apart: the registry, which decides who *owns* a block,
and the home's `.env`, which is what a starting process actually *reads*.

Found while auditing cluster homes during the #1059 investigation (#1075): two
worktree homes on the gateway host both claim `pg=18123 gw=18112`.

```
.ava-worktree-a                        pg=18123  gw=18112   (deregistered)
.ava-worktree-b                        pg=18123  gw=18112   (deregistered)
```

Every step was benign: the first worktree was destroyed, which freed the block; the second was born
later and was correctly given the now-free block. The allocator did its job. What
survives is a home directory that is invisible to the allocator and still fully
functional as a home.

## Decision

The refusal lives at the bring-up gate (`cli/preflight.py:require_installed_home`),
not in `ava cluster destroy`.

The gate already refused a home with no registry record, which covers the
destroyed home above. It now also refuses a home *with* a record whose port block
its `.env` contradicts, comparing three anchors — the outward gateway port, and
the Postgres and Redis ports read out of the connection URLs. Only ports named on
both sides are compared, so a record predating a port (prod's carries no
`pgbouncer`) is not drift.

`ava cluster destroy` keeps deleting nothing. A destroyed worktree's `.env` holds
the only copy of that cluster's secret, so making "free the port block" also mean
"lose the credentials" trades a recoverable state for an unrecoverable one. The
home stays on disk and stays un-bootable, which is the property that was wanted.

Two reasons for putting it at the gate rather than at destroy:

- It does not depend on the destroy path having run. A registry restored from an
  older snapshot, a hand-edited `.env`, or a home destroyed before this change all
  produce the same drift, and all are caught.
- It is checked where the consequence is. `.env` is read at bring-up; a marker
  written at destroy is a claim about the past that nothing re-verifies.

Verified against the live registry on the gateway host before making it a hard
refusal: all eight registered homes (prod and preview included) agree with their
records on all three anchors, so no existing cluster is refused by this.

## Alternatives rejected

**Have destroy rename the home's `.env` to `.env.destroyed-<ISO>`.** The issue's
first option, in its strongest form: it preserves the bytes, matches the
data-preservation posture, and every boot path fails on the missing file rather
than only `ava start`. That breadth is real and is the one thing the gate check
does not give.

Rejected on three counts. It destroys content that exists nowhere else, for no
coverage the gate does not already give: `.env` is the only copy of this
cluster's secret, of any key hand-added beyond `SEED_ENV_KEYS`, and — on an
enrolled runner — of the bootstrap facts its gateway auth rides on until a
re-enroll. It also carries the connection URLs, and the data-plane identity is
READ from them (`identity_from_url`, which raises rather than guess), so the file
is load-bearing for more than credentials. Second, "fails naturally on the
missing file" fails *generically* — a missing `.env` surfaces as a Settings
validation error, precisely the failure `cli/preflight.py` was built to replace
with an actionable message. Third, it only disarms homes destroyed after the
change; every home already on disk stays exactly as bootable as it is today.

One argument that looks decisive here is **not** true, and is recorded because it
is the natural thing to assume: that the secret must survive or the preserved pg
data dirs are stranded. They are not. `ensure_cluster_role`
(`shared/cluster.py:491-520`) re-sets the role's password to the *current*
cluster secret on every bring-up, as the instance's own initdb superuser over its
private loopback-`trust` socket — no old password is needed — and the redis ACL
is re-affirmed the same way. A preserved data dir (kept un-`initdb`'d by the
`PG_VERSION` short-circuit in `_ensure_pg_data`) is therefore reachable again
after a freshly minted secret; a rotation self-heals by design. The real cost of
losing `.env` is credential and configuration loss, not data loss.

**Put the refusal on the `.env` side because the gate's runner-only branch skips
the registry.** The sharpest argument for a `.env`-side mechanism, and it does
not hold: a runner-only home can never be a destroyed home. `install_cluster`
births a registry record only for a role containing `gateway` — a runner-only
install writes serve flags and returns — and `cmd_cluster_destroy` refuses any
home with no record. So every home that can ever be destroyed is gateway-capable,
which is exactly the population the registry branch covers. Both homes in the
census carry `AVA_MACHINE_SERVE_GATEWAY=true`, consistent with that. Pinned by
`test_destroy_cannot_reach_a_home_with_no_record`.

**Check every port in the block, not three anchors.** The per-service health
ports live in the record too, and mapping them to their `.env` keys means
reaching into `shared.env_keys` from a module whose whole discipline is being
settings-free (stdlib + dotenv reads). The three anchors are what a colliding
bring-up would actually bind — a data plane and an outward gateway — and any real
block reallocation moves them together.

**Refuse in the boot path (`shared/dotenv_boot`) so every entry point sees it.**
Broader, but it would refuse `ava status` and log reads on a home whose only sin
is a stale port, and the bring-up verbs are what can cause the collision. Left
unclosed and stated: `ava update` and `ava converge` do not run this gate.

## Not addressed here

The stale-home census from #1075 is untouched — this changes what a stale home
can *do*, not what is on disk. `~/.ava-worktree-c` (gw=18048, colliding with a live worktree's record),
`~/.ava-worktree-d` and `~/.ava-worktree-e` still point at a pre-cutover
shared Postgres on 5432, and `~/.ava-worktree-a` still holds the 18112/18123
block in its `.env`. All are refused at the gate now; sweeping the directories is an ops
task.
