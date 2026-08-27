# Data-plane URL host source is replaceable (de-hardcoded 127.0.0.1)

## Context

The per-cluster data-plane URLs (AVA_DB_URL / AVA_REDIS_URL) were born with a
hardcoded `127.0.0.1` host in `shared.cluster.derive.per_cluster_base_urls`,
and the admin/status dials in `cli/commands/_cluster_instance.py` and
`cli/commands/_data_plane_admin_secrets.py` dialed hardcoded loopback too. The
external-migration direction (Task #1752 — PG/Redis hosted off-box, SaaS or a
remote host) needs every URL and dial to follow a replaceable host source;
nothing may re-introduce a literal where the URL is the source of truth.

## Decision

- `ClusterRecord` gains `data_plane_host` (registry, default empty = loopback).
  `per_cluster_base_urls` renders both base URLs at that host — the record is
  the single derive-time source, never a literal.
- Birth snapshots the knob: `cli/commands/cluster_lifecycle._ensure_record`
  writes `settings.data_plane.data_plane_host` (new host-scope settings field
  `AVA_DATA_PLANE_HOST`) onto the new record. Existing records keep empty =
  loopback, so no existing cluster changes behavior.
- All admin/status dials read their host from the cluster's own URLs:
  `pg_isready` / `redis-cli` probes, the redis-py admin dials and the
  ACL-provisioning admin URL in `_data_plane_admin_secrets.py`, and the
  `ava status` data-plane probe (host + port both come from db_url/redis_url).
  The shared read is `shared.url_secret.url_host(url)` with a loopback
  fallback; at install birth the boot sentinels (loopback) stand in, so the
  rendered default is byte-identical to before.
- `shared/config/data_plane.py::_loopback_if_self` is kept as-is; its
  docstring now states the external-migration semantics: the rewrite is the
  self-dial posture only and must never be extended to foreign hosts — a URL
  naming another machine passes through and is dialed as-is.

## Alternatives rejected

A settings-only host (no registry field) was rejected: derivation happens at
birth, before the home's `.env` exists, so the record is the only durable
per-cluster source. Threading a host parameter through every dial site without
a shared `url_host` read was rejected as the same pattern repeated five times.

## Follow-up (QA PASS-with-nits, same day)

QA #3242 flagged two gaps, folded into a follow-up PR:

- `scripts/rotate_data_plane_secrets.py` still dialed literal `127.0.0.1` in
  four places (the pg/redis preflight+verify probes, the CONFIG SET dial, and
  the ACL-provisioning admin URL). `RotationState` now carries
  `pg_host`/`redis_host` (defaulted loopback, so a pre-upgrade rotation journal
  resumes unchanged), derived from the cluster's own URLs at `build_state`;
  every probe and dial threads them.
- The dial-layer host wiring had zero assertions (the same vacuous-gap family
  #724 caught): tests now assert the probes/dials receive the URL host against
  a FOREIGN host (loopback would match a re-hardcoded literal and prove
  nothing) for `ava status` probes, the bring-up probes, the credential-split
  admin dials, and the rotation script.
