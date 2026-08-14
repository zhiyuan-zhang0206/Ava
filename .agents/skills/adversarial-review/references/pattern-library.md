# Pattern Library — defect classes Ava has actually shipped

Every entry below is a class of defect that has occurred in this repo
(audit round-2 2026-08-08, three independent passes: 14 low-level domains,
10 cross-cutting themes, 7 independent Claude Code sessions; plus the
2026-08-03..08 incident series). When reviewing a diff, match it against
these classes. A structurally similar change is suspect until proven
otherwise. Each entry: symptom → how it reads in a diff → evidence anchor.

## A. Comparison & ordering

- **A1 Numeric fields compared as strings.** `"9.5" > "10.1"` is True.
  Watermarks, item ids, versions, cursors, indices compared with `>`/`<` on
  un-padded strings silently stop working past 9. (im_bridge SSE watermark —
  IM pushes stopped after the 10th message, P0, double-reported.)
- **A2 Sort order assumptions.** A consumer sorts numerically while the
  producer formats unpadded strings, or vice versa. Check both sides of any
  id/version ordering.
- **A3 Lexicographic boundary drift.** Someone "fixes" the padding later and
  the string comparison changes meaning.

## B. Polarity & fail-open

- **B1 Unknown reported as OK.** A probe whose read fails returns the
  "converged / healthy / passed" verdict instead of "unknown / retry".
  (`_probe_verdict` — DB read failure returned POLL_OK and released a
  deploy lease; "silence is not convergence".)
- **B2 Inverted legacy alias.** A rename flips semantics; the alias keeps
  the key but maps the value straight through, so old `.env` values mean the
  opposite of what they say. (`AVA_SKIP_AUTH` / `AVA_SKIP_SECURITY_SCAN` —
  skip=true enabled the thing it was supposed to disable.)
- **B3 Fail-safe comment, fail-open code.** Comment says "on failure run
  full CI"; condition evaluates to false on missing outputs and skips
  everything. (`changes` job in ci.yml — a failed changes job let PRs merge.)
- **B4 Fallback defaults below the real threshold.** A coverage/size gate
  whose env-var fallback is lower than the env value; deleting the env
  silently lowers the bar.
- **B5 Condition polarity on new gates.** `!=` where `==` was meant,
  `always()` chains with empty outputs, allow-lists that accept `skipped`.

## C. Silent failure & state advancement

- **C1 Top-level catch-all that swallows and continues.** The caller (or
  user) believes the operation happened. (`handle_inbound` — user messages
  silently lost when the gateway was unreachable; AtLeastOnce broken.)
- **C2 Cursor/offset advances on failure.** The poll position moves past the
  message that failed to deliver; it is never retried. (notice_bridge push
  failure, telegram offset on swallowed exceptions.)
- **C3 Batch write, whole-batch rollback, zero log.** One bad row fails the
  entire batch silently. (`_write_batch` FK violation — rows emitted, queue
  empty, no error, dropped=0.)
- **C4 Async called as sync.** An `await`-less call to an async function
  silently never runs the intended behavior (plus a "coroutine was never
  awaited" warning). (redis `disconnect()` no-op — reconnect path never
  executed.)
- **C5 Shared mutable watermark.** A per-agent watermark shared across
  channels/chats overwrites and suppresses pushes on the other channel.
  (`_last_pushed` global per agent.)
- **C6 Failure kills the process.** A recoverable error path terminates the
  agent/daemon with a non-resurrectable source, or a retry-able step has no
  retry policy while the analogous step does. (compact failure → agent
  permanently dead; termination_source='exit' not resurrectable.)
- **C7 Cleanup outside the failure path.** Rows claimed before the try stay
  `status NULL` forever; commits placed after the `with` block; `task_done`
  never called on shed items so `join()` always times out.
- **C8 Observer code amplifies outages.** Logging/telemetry that dials the
  DB exactly when the DB is down, or crashes on a bad format string, taking
  down the very thing it observes. (log.py rollout-quieting DB call; loguru
  `{}` placeholder TypeError killing the SSE reconnect path.)

## D. Retry & idempotency

- **D1 Hand-rolled retry loops.** A new retry loop with its own
  backoff/jitter/transient classification instead of converging on
  `shared/resilience.py` (design invariant D1 — "the only retry loop").
  The loops drift: one retries 429, another doesn't; one jitters, another
  sleeps negative.
- **D2 Retry re-executes non-idempotent work.** A timeout-then-retry of a
  browser click / form submit / send — the first attempt may have already
  executed server-side. Requires idempotency keys, not more retries.
- **D3 Two idempotency mechanisms.** A migration claims to unify two
  idempotency tables but creates a second one; cleanup policies and
  retention diverge.
- **D4 Idempotency keys permanently bricked.** Rows with `status NULL`
  never pruned (prune condition only matches completed rows) — the key
  ​​blocks every retry forever.
- **D5 Negative jitter.** `delay - span < 0` → `time.sleep(negative)` raises
  ValueError, masking the original error.

## E. Contracts & drift

- **E1 schema.sql out of sync with migrations.** The file that claims to be
  the schema truth lags a merged migration; fresh databases only work
  because every migration is idempotent. Any future non-idempotent
  migration fails on fresh DBs.
- **E2 Down migration destroys live data.** A `.down.sql` whose safety
  precondition (a mirror still writing) was removed by later code —
  rollback mechanically executes the down and drops live rows.
- **E3 Event contract vs emitter drift.** EventSpec declares payload keys /
  retention that the emitter never writes or the daemon never reads;
  "derived views live here and nowhere else" is false.
- **E4 Comment-as-contract lies.** schema.sql / OKF docs describe behavior
  the code no longer has ("mirrored by the emitter" — the mirror was
  removed; "cluster_update_lock is the transition signal" — code moved to
  deployment_state).
- **E5 restart_required mismatch.** Config fields whose consumer lives in a
  different process than the declared restart scope — changing the setting
  silently never takes effect.
- **E6 Renames with missed consumers.** URL/field/module renames that miss
  a test, a frontend type, or a docs reference — main goes red after merge,
  or docs point at ghosts.
- **E7 Relocation regressions.** Moving a module / daemon spec / parent
  index (`parents[N]`) without updating every string reference — the move
  neither errors nor self-heals. (task_maintenance watchdog respawn broke
  twice this way.)

## F. Scale & unbounded growth

- **F1 Dead data with no owner.** Cleanup only runs inside live processes;
  data owned by dead entities is never reclaimed. (36 GB of checkpoints,
  95% from terminated agents; DB was 26 GB and backups ~13 GB.)
- **F2 Append-only tables without retention.** New tables accumulate
  forever; the events table has partitions + TTL, everything else is
  unbounded.
- **F3 Cartesian / N+1 queries.** A list endpoint that joins a detail table
  without limit — 2760 agents x inbound rows = 50k intermediate rows, 1.7 s
  per call.
- **F4 Unbounded uploads / bodies.** `await file.read()` with no cap; no
  quota; disk-fill and OOM surfaces on any authenticated caller.
- **F5 Per-request expensive construction.** New event loop per request,
  full document graph rebuilt per request, full timeline rebuilt on every
  load.
- **F6 Scale-comment drift.** "at most a few dozen rows" comments that data
  has outgrown — the design assumption is the bug.

## G. Fake green & verification

- **G1 `|| true` in CI.** A flaky group whose failures are swallowed;
  "initially empty" comments that are no longer true. Structural fake green.
- **G2 Seconds-level full runs.** The entire suite finishing in seconds =
  the runner did nothing (0-byte uv incident). backend-parallel >= 4 min is
  the honest baseline.
- **G3 Silent skips.** Tests that skip when a dependency is missing, with
  no CI tripwire (pgbouncer tests skipped for days).
- **G4 Coverage sets drift.** CI `--cov=` list vs pyproject source list
  disagree; important packages outside the measurement entirely; branch
  coverage never measured.
- **G5 e2e without proof-of-work.** No junitxml/artifact assertion — a
  no-op e2e job stays green.
- **G6 Tests that reach the real world.** Tests invoking code that can
  POST to Telegram, register os_cron/launchd jobs, or apply migrations —
  the shell leaks prod `.env` into test processes; non-pytest scripts bypass
  conftest guards entirely. (Two P0 incidents: real Telegram pushes from
  pytest; a worktree debug script rewriting the prod health-probe plist.)
- **G7 Outage-merge red main.** Contract changes merged without CI (forced
  through an outage) leaving main red and blocking the whole queue. After
  any contract change, run the affected tests once locally before enqueue.

## H. Lifecycle & processes

- **H1 pidfile trusts process existence.** `kill(pid, 0)` accepts a reused
  pid; a stale pidfile blocks startup forever; writes are not atomic.
- **H2 Orphaned supervisors.** A daemon designed to supervise (page_server)
  is not actually running, leaving unsupervised processes behind.
- **H3 Orphan-reap namespace mismatch.** A cleanup pass matching by
  name/pattern kills the wrong processes (backend-only rollout reaping
  frontend+gate). Pattern kills (`pkill -f`) in tests/dev hit prod.
- **H4 Watchdog respawn broken twice.** Respawn paths that reference the old
  location or the wrong parent index — the self-healing chain silently
  dead.
- **H5 Lease vs sleep.** A pause path that outlives the lease gets reaped
  and force-killed on resume; in-flight turns lost. DB-outage pauses >10 min
  hit this.
- **H6 Token/secret on argv.** Secrets passed via command line (`--token`)
  visible to any local `ps`; the repo rule is env or 0600 files.
- **H7 Process escape.** Agent/daemon code that can outlive its session or
  supervisor; sessions that keep running after the agent is gone.

## I. User-ruling drift

- **I1 Ruling → code default mismatch.** A user ruling ("inspector starts
  closed") contradicted by the code default (`true`), because a later
  refactor changed the shape but not the default.
- **I2 Narration lag.** Code converges on a ruling; the roadmap/OKF/config
  UI/IM copy still describe the old world (users read a 5-day-old product).
  Every change that implements a ruling must update the narration in the
  same PR.
- **I3 Skill content language.** Skill bodies are English by ruling; CJK
  content or full-width punctuation in a skill is a finding.

## Cross-cutting meta-pattern

Almost every P0 above was **two independent passes finding the same thing**
(string comparison, swallowed exceptions, cursor advance, `|| true`,
schema drift) — and every one was invisible to CI. That is the whole case
for this skill: CI verifies mechanics; these classes live in the semantic
layer only an adversarial human-shaped review hunts.
