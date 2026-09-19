# Changelog

Notable changes, newest first. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Ava is pre-1.0; the
per-release PR-level detail lives in the annotated release tags (`git tag -n99`)
and the matching GitHub Releases, cut by `scripts/release_cut.py`.

## [Unreleased]

### Added
- `ava impersonate send <session_id> --agent <agent_id> --to <target> --content '<text>'`
  — the attested CLI form of sending to another agent as a leased identity
  (previously reachable only through the SDK attachment); it delivers source
  `agent:<id>`, the same borrowed identity the attachment stamps (task #4102).
- The `impersonation_aborted` telemetry event fires when the native supervisor
  stops a takeover after a core-component death, carrying the agent, lease,
  session, the dead component (executor / relay) and the detail (task #3998).
- Grafana ops monitoring for LLM stream stalls (task #3889, wiring the
  #3884 stall telemetry): the Ops dashboard's LLM section gains a
  "Provider stalls (per minute)" panel — `stream_stalled_retry` sliced by
  vendor plus the `stream_stall_pair_terminated` pair count — and the
  `ava-ops` alert group gains `ava-ops-llm-stall-pair` (fires on the first
  two-adjacent-stall termination) and `ava-ops-llm-stall-burst` (≥5 stalled
  streams per vendor per 15m; trailing-7d calibration: benign ≤2/15m, the
  2026-09-14/15 deepseek wave ran 2-9/15m). The pair event stays outside
  `LLM_ERROR_FAMILY` (it co-emits with the adjacent stall the family already
  counts); the new panel and rule are its display surface (task #3948).
- The cluster health probe gains two alert-only provider-account checks: a
  balance minimum (`AVA_PROVIDER_GUARD_BALANCE_MIN_CNY`, default 500 CNY)
  read from the provider's balance endpoint fires *before* the account runs
  dry, and a halted-agents check fires while at least
  `AVA_PROVIDER_GUARD_BLOCKED_AGENTS_MIN` agents sit halted by permanent
  provider rejections — both ride the existing 300s probe tick and edge-alert
  pipeline on the gateway host (task #3918).
- The watchdog gains a `browser-reach` check (agent-runner, browser-enabled
  hosts): a canary fetch through the shared Chrome — a throwaway background
  `about:blank` target closed in `finally` — contrasted with a same-process
  read of the gateway health URL, so "the browser's own network face cannot
  reach the gateway" (the 2026-09-18 stale-tab pool hang, task #3921) is
  detected instead of staying green. Report-only after
  `AVA_BROWSER_REACH_FAILURE_THRESHOLD` consecutive failing probes (one ERROR
  with both readings and the recovery pointer); throttled by
  `AVA_BROWSER_REACH_PROBE_INTERVAL_S`, bounded by `AVA_BROWSER_REACH_TIMEOUT_S`.
- Chrome pages created through the shared browser (an explicit `new_page`, or
  the auto-created page on a page-less first navigate) carry a hard TTL —
  `AVA_CHROME_PAGE_DEFAULT_TTL_SECONDS`, 24h default — and the browser-mcp
  daemon's sweep closes them once the deadline passes; activity never extends
  it. A daemon-owned `renew_page` tool, appended after the upstream tool list,
  moves the caller's page deadline to now + ttl (at most 24h per call); a page
  already past its deadline is not renewable. Only pages this stack created
  are ever inspected or closed, expiry surfaces as the existing no-page path
  rather than an invented "expired page" error, and each expiry/renewal emits
  a `chrome_page_ttl_expired` / `chrome_page_ttl_renewed` event (task #3035).

### Changed
- The ops-facing `ava` CLI moves its remaining argument checks to the parse
  layer (task #4092 batch B4, user ruling 2026-09-20): `schedules create`
  requires exactly one of `--script` / `--script-file`; `schedules update`
  and `presets update` require at least one field; `presets create|update
  --config` is validated as a JSON object at the boundary; `pitr drill`
  requires exactly one of `--chain` / `--candidate` and validates
  `--target-wall` as an offset-carrying ISO timestamp; `maintenance`
  validates `--operation` / `--acquired-at` up front; and `cluster
  update`'s `--target` / `--target-sha` combination checks (against
  `--restart-only` / `--local` / `--force` / `--dry-run` / `--mode force`)
  move from the command body to the parse layer. All of these are usage
  errors (exit 2) before any command code runs; the command-level guards
  stay as the programmatic-caller defense. Retained defaults (monitoring
  verbs, display bounds, safe-mode switches) now carry their reasons behind
  a `task #4092 cli-default inventory` marker; the discipline and the
  inventory entry point are documented in
  `conventions/cli-argument-discipline.md`.
- CLI parameter discipline, batch B3 (task #4092): `mcp add` requires exactly
  one of `--json` / `--command` and validates both at the parse layer (bad or
  non-object JSON, a `--env` pair without `=`, and `--arg`/`--env` without
  `--command` are usage errors), `mcp install --env` validates `KEY=VALUE`,
  and `ava packages policy` requires at least one field with `--check-every`
  validated at parse time.
- `ava impersonate request` / `renew` state their time parameters explicitly
  (task #4102): `request --ttl` and `--batch-window` and `renew --ttl` are
  required — missing values are usage errors before any command runs — the
  request's relay shape is validated at the CLI boundary, and the takeover
  launch message spells out the one-hour recovery deadline and immediate
  delivery it previously inherited from defaults.
- The Claude Code Monitor relay guidance now reflects per-watch deadlines
  (Claude Code 2.1.271+): arm with `timeout_ms: 1800000` and re-arm on the
  expiry notice — at the deadline the watch and the relay process it runs are
  killed, and a missed re-arm stops the lease (task #4037).
- A takeover no longer outlives a dead core component, and a dead relay is no
  longer silently respawned (task #3998, user ruling 2026-09-18): the accepting
  runtime re-checks the recorded executor process chain and the bound relay's
  heartbeat on every held pass — the dispatcher's pull scan now keeps held rows
  in its periodic cadence — and closes the lease when every anchor is
  dead/reused or the heartbeat is stale past 45 seconds: status `expired`, the
  cause recorded as `aborted: <detail>`, a relay process still held
  terminated, and an end-of-session note naming the cause. The single
  restart-shaped exception — a codex relay minted by an earlier incarnation,
  last beat before this process start, inside the fresh-start window
  (`AVA_IMPERSONATION_REPROVISION_WINDOW_SECONDS`, default 120 s, 0 disables)
  — is re-provisioned instead of stopped.
- The impersonation relay delivers routine inbox arrivals immediately unless a
  lease's merge window says otherwise: the per-lease window
  (`relay_batch_window_seconds`) is stated explicitly at request time
  (`--batch-window`, 0..300 seconds since task #4102); user chats, cancels and
  renewal reminders never wait in any case (task #3997).
- A macOS `ava start` whose own chain runs outside the GUI login session no
  longer brings services up in place (they would inherit the wrong launchd
  domain — the state that wedges the shared browser): an operator-shaped start
  hands the bring-up to the cluster's GUI-domain autostart job (`kickstart -p`,
  which never kills a running instance), waits for the job to reach its launch
  step, and answers with the same readiness exit contract as a normal start.
  Internal restart legs (update / `ava restart` / the stop compensator) keep
  their in-place behavior — their credential-handover marker cannot cross
  domains and their flags are not equivalent to the canonical job — and
  `AVA_START_GUI_HANDOVER=0` restores the previous warn-only behavior
  (task #3348).
- PR merge automation now uses Trunk Merge Queue: queued PRs are tested
  together in batches (target 4, max wait 5 min) via draft-PR runs on `trunk-merge/*`
  branches; the merge gate is the full CI job set (12 required statuses: the three
  suite aggregators, the non-matrix jobs, and the `qa-approved-gate` check); the
  `qa-approved` label remains the hard pre-merge gate (branch protection + queue
  admission).

### Fixed
- Inbound state semantics converge sooner and never double-show during a
  takeover (task #3999, from the #3683 duplicate display): every finished
  hosted turn now reconciles its own claimed `chat` rows at settlement — the
  throttled checkpoint tail is flushed first (`AVA_HOST_TURN_RECONCILE_ENABLED`)
  — so a handled message reaches `done` without waiting for the next boot or
  abort, and the pending strip (`GET /api/agents/{id}/pending`) hides a chat
  the active session absorbed: while an unexpired active session exists, a
  pending chat that the session trail transcribed or the relay read appears
  in the timeline only, and returns to the strip if the session ends
  unacknowledged. The row itself stays `pending`; the strip is a read-surface
  view.
- A `plugins_config.json` entry whose plugin directory is gone is now reported
  through the canonical plugin-load reporter (loguru ERROR + a
  `plugin_load_failed` event, once per process) instead of a plain warning —
  the dangling `codex_usage` / `deepseek_balance` entries on a 2026-09-11
  host stayed invisible to every alert surface while their plugins were
  silently disabled (task #3918).
- Hosted-force recovery no longer defers forever on an unreadable exec request
  envelope. An envelope whose own bytes cannot be read — the zero-byte or
  partial remnant a killed parent leaves — carries no attribution, so it is
  now bounded by the protocol: past twice the exec node timeout, with no live
  process reference and no live host process, it is quarantined (bytes
  preserved with a receipt) and counted, raising the
  `exec_request_bounded_quarantine` anomaly event; the retention switch is
  `AVA_EXEC_REQUEST_BOUNDED_QUARANTINE_ENABLED`. Exec envelopes are also
  written atomically (temp file + fsync + rename), so a killed writer cannot
  produce that remnant in the first place, and a boot recovery that stays
  deferred across three consecutive boots escalates to the
  `hosted_boot_recovery_stalled` anomaly event instead of one silent per-boot
  warning (task #3619).
- The hosted dispatcher re-asserts a pending cancellation after crossing its
  database scan: a cancel that lands inside psycopg_pool's async connection
  check is absorbed there (the pool returns the connection and retries without
  re-raising that `CancelledError` — upstream psycopg#1345, still present in
  3.3.1), which left the cancelled task looping forever with its canceller hung
  in `await task` (task #3513).
- Every automatic ava-browser rebuild now routes through the GUI domain when
  the healthcheck's own chain runs outside the macOS GUI login session — the
  session-gone sweep and the live-session dead-CDP respawn stop the stuck
  session and kickstart the cluster's GUI-domain autostart job instead of
  re-creating a context-less session in place (the residual path that kept the
  wrong-domain loop alive after the task #3149 heal). The rebuild line is a
  WARNING naming the trigger and a cumulative attempt count, service respawns
  record their chain's launchd domain for attribution, and `ava start` warns
  loudly when an agent-runner host is started from a chain outside the GUI
  login session (task #3346).
- A browser daemon chain that lost the GUI login session no longer waits
  forever for a Keychain it can never read: the macOS readiness probe names
  the missing context (`launchctl managername` ≠ `Aqua` — the state a respawn
  from an agent or SSH chain lands in, where securityd denies every
  login-Keychain query), and the healthcheck reacts by stopping the stuck
  session and kickstarting the cluster's GUI-domain autostart job instead of
  waiting in place — bounded to two attempts per episode, with its own
  in-context rebuild deferred while that relaunch lands. The CDP probe also
  stops treating every unusable `/json/version` answer as "respawn him": an
  answered port that is not this cluster's Chrome is `PORT_TAKEN` (no
  respawn churn), while our own wedged endpoint stays `DOWN` and heals
  through the existing sweep + rebuild (tasks #3149, #2692).
- Page ids are no longer trusted across an upstream reconnect: the browser-mcp
  daemon reconnects `chrome-devtools-mcp` in place, and the new process
  renumbers pages from 1 — a surviving page id could previously re-pin an
  agent's next call onto a different tab, or let the page-TTL sweep close one.
  Page-keyed state (per-agent affinity, TTL slots, the legacy per-connection
  page) now carries the upstream connection generation that minted it; after a
  reconnect stale slots read as no-page/expired and agents rebuild through the
  existing cold-start paths (task #3048).
- Listener discovery no longer reads a restricted inspection context as
  absence: the lsof fallback resolves through standard absolute locations when
  a context's PATH omits it (macOS keeps lsof in /usr/sbin, which cron's
  /usr/bin:/bin PATH never reaches — company-air `ava status` reported a
  healthy collector as "nothing listening on port 4318" for exactly this
  reason), and a psutil scan that cannot attribute the port's LISTEN socket
  (a `pid is None` row) falls back to lsof instead of reporting empty; when
  psutil sights a listener that lsof cannot confirm, discovery raises instead
  of certifying absence (task #2665).
- New shell session transcripts start with a unique host-written identity line,
  preventing filelog fingerprint collisions from shared login or CLI banners.
- Single-machine dual-unit telemetry port deviation: a unit whose local
  collector deviates (`AVA_TELEMETRY_OTLP_PORT` 4319, e.g. the WSL unit beside
  a Windows collector on 4318/8888) no longer derives its remote-station
  export target from its own port — station relays and probes use the station's
  advertised ingress (or `AVA_OBSERVABILITY_OTLP_PORT`), so telemetry stops
  black-holing to a port the station never listens on. Local collector liveness
  probes bind the unit's own receiver port independently of the producer export
  override, and a failed listener inspection reports `unavailable` instead of
  impersonating "nothing listening" (no unsafe respawn; task #2587).

- Maintenance drain receipts are graded by failure class: a turn raising a
  database-outage exception (`psycopg.OperationalError`, `PoolTimeout`)
  records a crash-equivalent `undelivered` receipt instead of a
  blocking failure — the hold stays resumable, and the host re-drives the
  held-control path (explicit re-flush before the restart claim) on the next
  wake, so a drain can never certify through an un-flushed tail. Blocking
  failures get a sanctioned exit: `ava maintenance repair --operation <op>
  --acquired-at <ts> [--operator ...]` moves them into an audited `repaired`
  record (operator identity + timestamp, both CAS sides visible in `ava
  maintenance status`) and releases the hold (2026-09-08 network degradation:
  38 PoolTimeout receipts permanently wedged the machine with no repair path).
- Plugin loading is fail-soft: a plugin whose `plugin.py` raises at import (or
  a config entry whose plugin directory is gone) is skipped with a loud
  `plugin_load_failed` event + log instead of crashing `import ava` cluster-wide
  (2026-08-28 ava_ledger incident). External plugins' `from . import x` relative
  imports now resolve via a framework-registered `plugins.<name>` namespace
  regardless of sys.path/cwd, and `ava plugins install`/`upgrade` land the
  plugin tree atomically (staging + rename, previous version restored on
  failure) so a half-installed plugin can no longer exist.
- Plugin-load containment now covers every load path, not just the graph build
  (issue #2161, user ruling 2026-09-11). The host-boot loader imports through
  the same primitives as the graph-build loader — same dotted module name, same
  `sys.modules` identity, same fail-soft skip + loud `plugin_load_failed`
  report — so a broken `plugin.py` can no longer take the agent host down on
  every restart (2026-09-10 agent-host incident), and a plugin disabled in
  `plugins_config.json` is imported by no production path (the boot loader
  previously ignored the enable set). The same per-plugin containment now
  covers `provider.py`, `services.py`, `setup.py`, a built-in `metrics.py`, and
  the launched child's `import ava` self-load. Deliberately still fail-closed
  (no guess at operator intent): duplicate plugin names, a malformed
  `plugins_config.json`, plugin-config schema drift, provider
  registration-contract violations, and the provider loader's post-load
  cross-model revalidation. The release probe substitutes the canonical
  fail-soft reporter, so a candidate image with unloadable plugin code is
  rejected instead of degraded.
- Release preparation now exercises provider registration alongside the
  `plugin.py` and `services.py` faces: the probe keeps the image's own
  provider-bearing built-ins enabled next to the retained set (the provider
  loader rejects an empty binding set), so a retained `provider.py` that
  fails to load rejects the candidate instead of shipping and silently
  degrading the model registry after rollout (task #3008).
- Session-revoke suffix fallback could revoke the request's own session when
  called with its masked suffix (the logout-only guard was bypassed) and
  accepted any id shape of 8+ characters as a suffix. The fallback now only
  triggers for the exact 8-character masked form and excludes the current
  session from matches.
- The `qa-approved-gate` label check now reads the PR's labels live from the
  API at evaluation time instead of the event payload: a run queued before the
  label was applied can no longer execute late and flip the check's newest
  result back to failure after the labeled run passed, and a stale payload
  that still lists a removed label can no longer pass. An unlabeled PR is
  still red; the Trunk exemption is unchanged (task #3334).

### Added
- Browser-mcp daemon now keeps a valid gateway session cookie in the shared
  managed Chrome: logs in through the gateway, injects the returned opaque
  server-side session over CDP (`Network.setCookie`), and refreshes every 6h.
  The managed browser can open auth-gated gateway URLs (agent-served pages
  behind the gateway reverse proxy) on any machine, fresh profile or not.
- The browser-session list masks non-current session ids to their final 8
  characters (the revoke endpoint accepts the suffix) and labels
  managed-browser sessions; the managed browser re-checks its gateway session
  after gateway-URL navigations and refreshes early when it no longer
  authenticates.
- Delivery observability: `inbound_messages.claimed_at` (set on claim; pickup
  latency = claimed_at - created_at), degraded idle-wake logged at WARNING when
  the pub/sub fast path is lost, and a gateway delivery watchdog that alerts on
  chat inbounds still pending past 30s (once per row while stuck).
- Delivery watchdog now also **dispatches lost wakes**: on a 0.5s tick it
  re-publishes the Redis wake (with the wake-key breadcrumb) for every pending
  inbound of an idling owner older than 1s — a lost pub/sub publish recovers in
  ~1.5s instead of the claim loop's 30s recheck. Constant ~2 qps load,
  independent of fleet size.
- Open-source readiness: Apache-2.0 license + third-party notices,
  community-health files (CONTRIBUTING / CODE_OF_CONDUCT / SECURITY / templates),
  and a forkable CI lane for GitHub-hosted runners.
- `install.sh --mirror cn`: route PyPI / npm / Homebrew through China mirrors.

### Changed
- Cross-machine file transfer no longer hard-requires a shared Google Drive
  folder: `AVA_REQUIRE_GOOGLE_DRIVE` is replaced by the configurable
  `AVA_CROSS_MACHINE_TRANSFER_BACKEND` (`drive` | `none`, default `drive`), and
  the agent-runner converge step probes the backend and warns instead of
  blocking start when Drive is unavailable.
- The local trace mirror now stays disk-bounded end to end: the collector's
  file exporter rotates `spans.jsonl` at 64 MiB (was 256) with 24 backups,
  old segments are gzipped to `.jsonl.gz` by an agent-start pass (5-10x
  smaller), and the agent-side retention/dir-cap prunes now recognize the
  timberjack-suffixed rotated names (`spans-<ts>-size.jsonl`), the manual
  `spans.cut-*` shape, and `.gz` variants — previously those files were
  invisible to retention and could even outlive the active file in a cap
  prune. `ava trace ship` and the inspect-a-trace mirror fetcher read gzipped
  segments transparently, continuing from the same per-file watermark.
- Pure agent-runners now relay OTLP traces/logs/metrics through a
  bearer-authenticated gateway collector receiver instead of writing to
  nonexistent loopback LGTM backends. Tempo/Loki/Prometheus stay gateway-local;
  collector queue/drop/silence alerts cover delivery failures.
- Session stdout is ingested into Loki through disjoint collector filelog
  receivers: agent shells separately from gateway/daemon/schedule output, with
  banner-only agent main logs excluded. In coordination with #3279, 10-second
  polling, EOF metadata archival, bounded discovery, and daily-throttled local
  7-day `*.out.log` retention prevent content-fingerprint re-watch storms.
- Genericized author/prod identifiers (container registry, host addresses, repo
  slugs) out of code, CI, and tests.

### Removed
- `ava logs` CLI (list live sessions / tail one session's log) — replaced by
  the Loki query path above; see `deploy/lgtm/README.md`.

### Security
- Secrets no longer ride process command lines. Session env is handed to the
  child out-of-band (a 0600 env file, never an argv splice), the per-agent
  config overlay travels in the child's environment, and the cluster's
  redis takes `requirepass` from a config file / `$REDISCLI_AUTH` — so `ps` no
  longer shows the cluster secret, the data-plane URLs, or provider API keys to
  other local users.
