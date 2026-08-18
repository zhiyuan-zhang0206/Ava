# An AVA_HOME that contradicts the checkout's own claim is refused, not resolved

## Context

`resolve_ava_home()` ranked the `AVA_HOME` env var above the executing
checkout's `.ava_home` pointer. Both are legitimate inputs and they normally
agree, so the ranking looked like a tie-break. It is not: the env var says which
cluster **launched** this process, the pointer says which cluster's code it is
**running**, and when they disagree the process holds half of each cluster.

That is the second path into the 2026-07-31 prod wedge (#1059), and the one that
generalizes. From `~/.ava/logs/agent-2367.log`, 04:44:32 PDT: fleet agent 2367
ran `install.sh --worktree`, which did everything right — including writing the
worktree's `.ava_home` — and then ran `cd <worktree> && .venv/bin/ava start`. The
agent's shell inherits `AVA_HOME=/Users/ava/.ava` from the prod session
launch env, so the env var beat the pointer written two seconds earlier: the
start reported the prod gateway, and the worktree's newer `migrations/` were
applied to the central prod database.

The detonator is `install.sh --worktree`'s own printed next step — run `ava
start` from the checkout — which is correct advice that is silently wrong inside
an environment carrying another cluster's `AVA_HOME`. Every fleet agent runs in
exactly that environment, so the phantom-cluster incident class the
checkout-anchored boot was built to end was live again for the whole fleet.

## Decision

When `AVA_HOME` is set, the checkout claims a home of its own (the prod-source
path rule or a `.ava_home` pointer), and the two name different paths,
`resolve_ava_home()` raises `AvaHomeContradictionError` naming both paths, which
rule produced the claim, and the two fixes (`unset AVA_HOME`, or run the other
cluster's checkout).

Refusing rather than picking is the point. Whichever side wins, the other half of
the process's world comes from the loser: the `.env` (database URL, cluster
secret, port block) from one cluster and `migrations/`, skills and plugin images
from the other. There is no resolution that is right — only a caller who has not
said which cluster they mean.

It raises at import of `shared.dotenv_boot`, which resolves the home into a
module constant. That is early enough to precede `load_ava_env()`, so a refused
process never even has the wrong cluster's credentials in `os.environ`.

`AVA_HOME_OVERRIDE=1` authorizes the contradiction for the three callers where
mixing is the intent, each of which sets it on the specific invocation:

- `cli/install_cluster.py` — it *writes* the pointer, so pinning `AVA_HOME` to
  the install target has to outrank the pointer the run is about to replace.
- `cli/commands/cluster_lifecycle.py:_subprocess_env` — `ava cluster
  down/destroy` runs this checkout's `cli.main stop` against another cluster's
  home by design. Stopping is home-scoped and reads nothing from the target
  checkout, so the dangerous mixing cannot occur.
- `tests/conftest.py` — the suite redirects to a scratch home before the first
  project import. Without the exemption the suite would fail to import on any
  worktree carrying a pointer (i.e. every worktree with its own dev cluster)
  while passing on a fresh CI clone, which has none.

The hatch cannot become ambient in production: the check reads the real process
environment before any `.env` is loaded, so no cluster can grant itself the
exemption on disk, and nothing in the prod session env or the install writes it.

## Relationship to the migration authority guard

[#1071](2026-07-31-migrations-are-gateway-only.md) is the narrow backstop —
it catches the same accident at the one moment it tries to migrate, by comparing
`checkout_anchored_home()` against the identity the database carries. This is the
general fix: the process stops before it opens a connection at all. They are
complementary, and this one also closes #1071's residual gap, where a checkout
whose pointer *explicitly names* the prod home would pass the authority check.

Both are built on the same `checkout_anchored_home()` split, which is why this
is one fold rather than a second parallel notion of "which home is really mine".

That doc's Consequences section left this open deliberately: "`resolve_ava_home()`
keeps its `AVA_HOME`-first precedence. Narrowing it was considered out of scope
here: too many launch paths depend on the env var winning, and the migration
guard no longer needs it to." Both halves still hold. The guard does not need
this change — it reads `checkout_anchored_home()` and is unaffected either way.
And the deferral's stated blocker was that the dependent launch paths were not
enumerated; this decision does that work, finds exactly three that legitimately
run a checkout against a home it does not own, and exempts each one explicitly
rather than by weakening the rule. Narrowing is scoped to the contradiction —
every launch path where `AVA_HOME` is unambiguous still has the env var winning,
unchanged.

## Alternatives rejected

**Prefer the pointer over the env var.** Inverts the precedence instead of
questioning it, and breaks every legitimate `AVA_HOME`: a gateway-launched
daemon, an enrolled runner, `ava cluster down` acting on another home. It also
keeps the failure silent — the process would just quietly act on the other
cluster.

**Strip cluster-identity env keys in `ava.shell` when the cwd is not the prod
checkout.** Deferred rather than rejected (it is the ergonomic fix — the agent's
command would then do what it reads like it does), but it does not subsume this
guard. It has to decide *which* keys are cluster identity, and that set drifts;
a missed key gives a subtler split-brain (right database, wrong secret). It only
covers commands routed through `ava.shell`, not a bare `subprocess.run` in agent
code, a `Bash` tool call, or a human in a leftover shell pane. And it changes
behavior for every workflow that legitimately inherits that env. Sequencing:
land the fail-fast first, then evaluate stripping on top with this as the net
that proves it works.

**Fire only when the env home looks "live" (has a `.env`, or a registry
record).** Would have exempted the test suite with no new env var, since a
freshly-created scratch home has neither. Rejected as a heuristic standing in
for authorization: an e2e test whose scratch home *does* have a `.env` is
indistinguishable from the incident, and reading the registry would put a
host-global JSON read (and its failure modes) in the boot path of every process.
An explicit opt-in says the true thing — this caller means it — and is greppable.
