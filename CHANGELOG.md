# Changelog

Notable changes, newest first. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Ava is pre-1.0; the
per-release PR-level detail lives in the annotated release tags (`git tag -n99`)
and the matching GitHub Releases, cut by `scripts/release_cut.py`.

## [Unreleased]

### Added
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
- PR merge automation now uses Trunk Merge Queue: queued PRs are tested
  together in batches (target 4, max wait 5 min) via draft-PR runs on `trunk-merge/*`
  branches; the merge gate is the full CI job set (12 required statuses: the three
  suite aggregators, the non-matrix jobs, and the `qa-approved-gate` check); the
  `qa-approved` label remains the hard pre-merge gate (branch protection + queue
  admission).

### Fixed
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
