---
type: doc
title: "Operator-approved tool pins"
description: "The no-lockfile tool pins in base/host/brew_pin.py: the Homebrew formula manifest, the canonical uv release with its asset digests, the apt-series pins, and the warning-only brew probes."
tags:
  - base
  - ops
---

# Operator-approved tool pins

`base/host/brew_pin.py` owns the operator-approved tool pins that have no lockfile: the
Homebrew formula manifest, the uv release (version plus the four platform asset digests) and the
apt-series pins for PgBouncer and Redis. The brew probes are warning-only drift detection for
`ava converge`: an absent brew or a process error degrades to `None`/empty sets, a missing or
unpinned formula is reported while an uninstalled one is not, nothing mutates a pin, and no work
runs at import time. `brew_prefix` falls back to `/opt/homebrew` when brew is absent so callers
read a harmless path instead of crashing, and the `brew list` subprocess is bounded by
`_BREW_TIMEOUT_S` so a wedged brew cannot stall lifecycle commands.


```ava-contract
id = "brew-formula-manifest"
kind = "invariant"
statement = "PINNED_BREW_FORMULAE is the operator-approved Homebrew formula manifest; unpinned installed members are reported and uninstalled members are not."
code = ["base/host/brew_pin.py:PINNED_BREW_FORMULAE"]
tests = ["cli/commands/lifecycle/tests/converge_host/test_converge_brew_pin.py::test_pinned_formula_manifest_is_exact", "cli/commands/lifecycle/tests/converge_host/test_converge_brew_pin.py::test_uninstalled_manifest_formula_is_not_flagged"]
```

```ava-contract
id = "uv-pin-copies"
kind = "invariant"
statement = "UV_VERSION and UV_ASSET_SHA256 are the canonical uv pin: scripts/provision/toolchain.sh and every setup-uv workflow step embed the same version, toolchain.sh embeds the same digests, and drift fails the pin contract tests."
code = ["base/host/brew_pin.py:UV_VERSION", "base/host/brew_pin.py:UV_ASSET_SHA256"]
tests = ["tests/scripts/test_toolchain_uv_pin.py::test_toolchain_embeds_the_canonical_version", "tests/scripts/test_toolchain_uv_pin.py::test_toolchain_sha256_map_matches_the_canonical_set", "tests/scripts/test_toolchain_uv_pin.py::test_every_workflow_setup_uv_pins_the_canonical_version"]
```

```ava-contract
id = "apt-pin-copies"
kind = "invariant"
statement = "PGBOUNCER_APT_VERSION and REDIS_APT_VERSION are the canonical apt-series pins; scripts/provision/database.sh and the CI install steps embed the same strings and drift fails the pin tests."
code = ["base/host/brew_pin.py:PGBOUNCER_APT_VERSION", "base/host/brew_pin.py:REDIS_APT_VERSION"]
tests = ["tests/ci/test_pgbouncer_pin.py::test_provision_script_installs_the_canonical_pgbouncer", "tests/ci/test_pgbouncer_pin.py::test_ci_installs_the_canonical_pgbouncer", "tests/ci/test_redis_pin.py::test_the_canonical_pin_is_the_approved_8_2_series", "tests/ci/test_redis_pin.py::test_provision_script_installs_and_holds_the_canonical_redis", "tests/ci/test_redis_pin.py::test_ci_installs_the_canonical_redis_from_the_redis_io_repo"]
```

```ava-contract
id = "brew-probe-warnings"
kind = "invariant"
statement = "The brew probes are warning-only drift detection: an absent brew or a process error degrades to None or an empty set, and the converge step stays silent when brew is absent or non-macOS while reporting each missing or unpinned formula."
code = ["base/host/brew_pin.py:pinned_brew_formulae", "base/host/brew_pin.py:installed_brew_formulae", "base/host/brew_pin.py:unpinned_formulae"]
tests = ["cli/commands/lifecycle/tests/converge_host/test_converge_brew_pin.py::test_all_formulae_pinned_is_silent", "cli/commands/lifecycle/tests/converge_host/test_converge_brew_pin.py::test_missing_formula_warns_with_manual_repin_command", "cli/commands/lifecycle/tests/converge_host/test_converge_brew_pin.py::test_brew_absent_is_silent", "cli/commands/lifecycle/tests/converge_host/test_converge_brew_pin.py::test_non_macos_is_silent_without_calling_brew"]
```

Parent: [[base/host/docs/host.ava.okf.md]].
