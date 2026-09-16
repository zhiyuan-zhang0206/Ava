# Config boot-lite: import prepares a lazy layer, first touch upgrades

## Context

Startup cost of the config chain dominated clean `import ava` and every hosted
exec child. `import shared.config` built the full 391-field registry and
constructed the `Settings` singleton before any caller read a field, pulling in
15 sub-modules, `pydantic_settings`, and the pydantic_core/TypeAdapter tails —
spike #3612 measured +24.0MiB of child marginal RSS for the config chain (the
L1 bootstrap change removed the 10.5MiB httpx share). Most processes read only
a small boot-path subset of the fields; exec children are the extreme case.

Constraints set by the 405 ruling (2026-09-16): the default applies to all
process classes (not a per-class whitelist); the boot-path field index must be
generated and guarded by drift assertions; the settings-lite
`AVA_CONFIG_FETCH=skip` path stays out of scope and unchanged. An adversarial
review (#6303) of design v1.1 found and the implementation fixed two blockers
before merge: the hosted child's *real* pin map (10 keys, 7 off-index) must not
force an upgrade, and two direct-submodule import edges had to be cut for the
"no sub-modules, no `pydantic_settings`" claim to hold.

## Decision

`import shared.config` prepares a boot-lite layer (`shared/config/_lite.py`)
and constructs the eager chain (`shared/config/_full.py`) only on the first
access the lite layer cannot serve.

- The boot-path index is a generated artifact: `shared/config_lite_table.json`
  is produced from the live registry by `scripts/gen_config_lite_table.py` and
  read by the hand-written `shared/config_lite_table.py`. It lives outside the
  `shared.config` package because `shared/env_registry.py` consumes the same
  surfaces before Settings exists. The `config-lite-table-fresh` gate compares
  the committed bytes against a regeneration in pre-commit and CI, so index
  drift is a red gate, not a runtime surprise.
- Lite serves a curated 21-row field table from the environment (plus pending
  overlay writes). Any other read upgrades once: build the sub-models,
  registry and `Settings` under the existing lock, replay the pending overlay
  writes in order, and rebind the facade's `settings` name to the constructed
  singleton. Bindings taken before the upgrade keep working through the stable
  view; a re-entrant read during the build gets a documented "in flight" error
  instead of a second build.
- The exec-child boot path stays lite. The per-agent pin overlay reaches
  `set_field`, which records any registered field as a pending override and
  never forces the upgrade; `sdk_disable`, read at child boot, is in the index.
- Full-validation processes opt in explicitly: `ensure_eager()` (the gateway,
  ops daemons and agent host call it) and the operator escape hatch
  `AVA_CONFIG_BOOT=eager` (process env only; `skip` wins over it, so the
  settings-lite repair verbs stay usable even under a global eager flag).
- Startup validation shrinks to the boot-path fields; a field a process never
  touches is no longer validated at import. Local-source units keep their
  import-time required-field check, a configured runner still fetches the
  gateway bootstrap at the same point, and the equivalence criterion is
  first-error parity (stage, exception, locus) with construction-order
  validation, with the W1--W4 windows documented in the design record.

## Alternatives rejected

- **Defer only into the existing skip proxy.** Skip kept the full import
  closure and registry build, so the spike showed no meaningful saving; ruled
  out by 405.
- **A per-process-class whitelist (lite for exec children only).** Rejected in
  favor of the all-class default: one mechanism, no class inventory to keep
  current; classes that need full validation opt in explicitly.
- **A generated `.py` module for the index.** The all-field faces alone exceed
  the repo's 800-line hard ceiling (no exemption), and the ceiling's remedy
  (split into modules) does not fit a table whose columns are never consumed
  separately. A JSON data file carries no line budget (precedent:
  `shared/lm/pricing_catalog_archive.json`).
- **Upgrading on any overlay write, or writing pins through at boot.** The
  hosted child receives the agent's whole frozen pin map and reads
  `sdk_disable` after applying it; upgrading on the first such write would
  have nullified the entire child saving. Pending + replay keeps overlay
  semantics identical without the build.
- **Snapshotting lite field values at prepare time.** Lite reads the current
  environment (the eager singleton froze values at construction). No
  production code mutates boot-path env after import; an invariant test pins
  this behavior instead of a snapshot mechanism.

## Consequences

- Import no longer validates the 372 off-index fields; a broken off-index value
  surfaces at first touch — or not at all in a short-lived process. Mitigated
  by `ensure_eager()` on the resident classes, the required-field check, and
  tests pinning first-error parity with the eager construction order.
- The generated index and the facade's lazy-name latch are new maintenance
  surfaces, each gated: `--check` for the index, old-name parity plus pickle
  tests for the latch, and escape-hatch combination tests for `eager`/`skip`.
- Measured in-context retest (2026-09-16, spike recipe, clean env x3): clean
  `import ava` -4.5MiB (75.5->71.0), crafted child start -4.8MiB
  (109.3->104.5); child `pydantic_settings` absent, shared.config modules
  29->6. L1 (the child-batch startup change) squash-landed on main as
`0a17312f6`; measured against that L1-inclusive baseline (main@66fc357df:
67.5MiB clean / 108.2MiB child start), the rebased branch delivers the
combined result - 63.3MiB clean / 101.9MiB child (-4.2 / -6.2MiB on the
same baseline). The spike's isolated-marginal projections (-10.7 child / -13.2
  clean) are void: they double-counted the pydantic base that seven non-config
  consumers keep resident (`ava.security`, `ava._mcp_oauth`,
  `shared.live_events`, `shared.agent_snapshot`, `shared.audit_events`,
  `shared.install_registry`, `shared.agent_observation`). The <=58/<=98 series
  targets are repositioned onto the lazy total-account trajectory (gap ~5MiB;
  405 ruling 2026-09-16 14:03) - not gating this change. The design record
  `CONFIG-BOOT-LITE-DESIGN.md` (v1.3, section 7.1) carries the windows,
  the equivalence criterion, and the corrected numbers.
- The legacy facade surface stays complete for repair-side consumers: five
  settings-free re-exports, a call-time `refresh_data_plane_settings` shim,
  and registry/metadata-backed field faces (`_FIELDS`, `FIELD_INFOS`,
  `CONFIG_UNCHANGED_SENTINEL`, `ConfigFieldMeta`) serve without the upgrade,
  so settings-lite verbs (`ava config set`) keep repairing a broken `.env`;
  enforced end-to-end by the suite (`tests/cli/test_config_cmd.py`).
