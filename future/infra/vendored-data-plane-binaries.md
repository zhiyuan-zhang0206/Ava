# Vendored data-plane binaries (drop the brew/apt prerequisite)

**Status: Postgres leg landed; Redis leg is the only remaining work.**
`shared/runtime_binaries.py` fetches the pinned relocatable zonky distribution into
`~/.ava/runtime/pg/` (a `cli/commands/_converge.py` step), and
`shared/pg_tools.py:pg_tool()` prefers it over brew/apt. So `brew install
postgresql@17` is no longer a prerequisite. `redis-server` is still resolved from
brew/apt (`cli/commands/_cluster_instance.py`), which is what keeps a package
manager on the critical path.

This is also **the single home for the remaining slice 3** of
[`embedded-per-cluster-data-plane.md`](embedded-per-cluster-data-plane.md) (that doc
is the design record of the per-cluster instance model; it points here rather than
re-describing the bundling).

## Goal (scope: light)

A clean machine runs Ava without first installing Postgres/Redis through a package
manager. Not a consumer `.app`, not distribution-to-others — just a self-contained,
reproducible install on the operator's own machines. The data plane is already
Ava-owned and per-cluster; this removes the last host-package dependency under it.

## Why brew binaries can't just be copied

Homebrew's `postgres` links its libs by **absolute path** (`/opt/homebrew/opt/icu4c/...`),
so a copied keg breaks on any machine without that exact layout. A relocatable
distribution links via `@loader_path/../lib` (macOS) / `$ORIGIN/../lib` (linux), so
`bin/ lib/ share/` runs from anywhere. That is why we source a purpose-built
relocatable distribution, not the brew keg.

## Postgres source: zonky `embedded-postgres-binaries` (landed)

`io.zonky.test.postgres:embedded-postgres-binaries-<platform>` on Maven Central —
relocatable, reduced-size PG, used widely for embedded testing. Verified against
`darwin-arm64v8:17.4.0`:

- The Maven artifact is a `.jar` (a zip) containing one `postgres-<platform>.txz`.
  Recipe: download jar → extract the inner `.txz` → untar (xz) into the runtime dir.
- Fully relocatable: `otool -L` shows every third-party lib (icu, openssl, zstd,
  lz4, xml2, krb5, z) bundled under `@loader_path/../lib`; only ever-present system
  libs (`libSystem`, `libpam`, the LDAP framework) are external. `initdb`/`postgres
  --version` run standalone from the extracted dir.
- The darwin artifact is a **universal binary** (x86_64 + arm64) — one mac artifact
  covers Intel + Apple Silicon.
- Platforms we pin: `darwin-arm64v8`, `linux-amd64` (the operator's real targets).
  The `.jar` URL on `repo1.maven.org` is stable + content-addressable; pin the
  version + a sha256.

## Redis source: a prebuilt we publish (option A) — REMAINING

Redis core has no external dependencies (bundles jemalloc/lua); built `BUILD_TLS=no`
the `redis-server` binary links only system libs, so a prebuilt is relocatable. We
publish one per platform as a GitHub release asset from a CI build job, and the
install downloads it — symmetric with the PG download, **no compiler at install**
(the rejected alternative, build-from-source, would trade the brew prerequisite for
a toolchain prerequisite). The CI build-and-publish pipeline is the one piece of new
infra this introduces.

## Where the binaries live + how they're resolved

- **Location:** `~/.ava/runtime/{pg,redis}/` — host-level, **shared across every
  cluster and checkout** (the binaries are read-only; only the *data* is
  per-cluster). Independent of any `$AVA_HOME`, so one download serves the whole
  box. Version-stamped so an upgrade can drop a new tree beside the old. `pg/` is
  live; `redis/` is the remaining leg.
- **Resolution:** `shared/pg_tools.py:pg_tool()` prefers `~/.ava/runtime/pg/` when
  present, else falls back to brew/apt — so existing dev boxes keep working
  unchanged and a clean machine uses the vendored copy. The redis bin resolver
  (`cli/commands/_cluster_instance.py`) still only knows brew/apt; it gains the same
  prefer-vendored shape with the redis leg.
- **Version pinning:** the PG major must match across `initdb` and the data dir it
  created, so the pinned version is a single source of truth; a major bump is an
  expand step (new tree, re-`initdb` or `pg_upgrade`), not an in-place swap.

## Fetch mechanism

A `cli/commands/_converge.py` step (`ensure_pg_binaries`) downloads + extracts +
checksums on first install or when the runtime tree is missing (idempotent, like the
rest of converge). Not committed to git (the PG tree is tens of MB). A download
failure is fatal with a clear message (fail fast — no silent fall-through to a
half-present runtime). The redis leg reuses this step.

## Slices

1. **✅ Done — Postgres vendoring.** zonky download + extract + checksum into
   `~/.ava/runtime/pg` (`shared/runtime_binaries.py`), fetched by the converge step;
   `pg_tool()` prefers the vendored tree, falling back to brew/apt when absent. Every
   cluster instance (`_cluster_instance.py`) `initdb`s and serves via `pg_ctl`
   directly, so the pg half of the per-cluster data plane is brew-free.
2. **Redis vendoring — remaining.** The CI build-and-publish pipeline (per-platform
   `redis-server`, `BUILD_TLS=no`, published as a release asset) + the download +
   the prefer-vendored resolution in `_cluster_instance.py`.

Split because the two have different risk: PG reuses an existing third-party
artifact (download only); redis stands up our own build-publish pipeline. That is
also why PG went first — it is the heavy prerequisite, and redis via brew is a small
single binary, so the remaining leg buys the *last* step of "no package manager"
rather than the bulk of the value.

## Out of scope

- Consumer `.app` packaging (signing / notarization / app wrapper / non-git
  auto-update) — a separate product decision, not this.
- Windows — the `gateway` capability is POSIX-only by decision, so there is no
  native Windows pg/redis to vendor. Redis is the binding constraint and it is
  the first item in [`windows-gateway.md`](../../gateway/windows-gateway.md).
- Distribution to third parties — the vendored binaries make it *possible* later,
  but the goal here is the operator's own machines.
