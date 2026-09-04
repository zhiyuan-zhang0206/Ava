# Changelog

Notable changes, newest first. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Ava is pre-1.0; the
per-release PR-level detail lives in the annotated release tags (`git tag -n99`)
and the matching GitHub Releases, cut by `scripts/release_cut.py`.

## [Unreleased]

### Changed
- PR merge automation now uses Trunk Merge Queue: queued PRs are tested
  together in batches (target 4, max wait 5 min) via draft-PR runs on `trunk-merge/*`
  branches; the merge gate is the full CI job set (12 required statuses: the three
  suite aggregators, the non-matrix jobs, and the `qa-approved-gate` check); the
  `qa-approved` label remains the hard pre-merge gate (branch protection + queue
  admission).

### Fixed
- Plugin loading is fail-soft: a plugin whose `plugin.py` raises at import (or
  a config entry whose plugin directory is gone) is skipped with a loud
  `plugin_load_failed` event + log instead of crashing `import ava` cluster-wide
  (2026-08-28 ava_ledger incident). External plugins' `from . import x` relative
  imports now resolve via a framework-registered `plugins.<name>` namespace
  regardless of sys.path/cwd, and `ava plugins install`/`upgrade` land the
  plugin tree atomically (staging + rename, previous version restored on
  failure) so a half-installed plugin can no longer exist.
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
