"""The closed lists of the ambient-state rule (meizel/state/ambient_state/__init__.py).

Everything the rule lets through without a baseline entry is listed in `meizel.policy.toml`, in
the `[ambient-state]` tables, and every entry that names a file or a site says why. This module
only reads them; it holds no entry itself. The lists are closed: a new entry is a deliberate edit
of the policy with a one-line reason, never an inline comment at the site, and `python -m meizel
policy diff` reports it. Entries that name a file or a site go stale — the gate fails when the
file is gone or no longer holds the thing — so a list cannot rot into a permission wall.

Three families are kept, and only these (user ruling 2026-10-02):

1. **Write-only facades** — loggers, meters, tracers, and the internals of the
   log/telemetry sinks. Code writes to them and nothing reads them back to decide
   what to do (`SINK_CALLEES`, `SINK_CALLEE_SUFFIXES`, `SINK_FACADES`).
2. **Framework wiring** — an `app`/router/parser/graph definition and the
   synchronization primitives. A lock is wiring; the state it protects is not
   (`WIRING_CALLEES`).
3. **Pure constants** — a value computed from literals that cannot change an
   answer: a compiled regex, a `TypeVar`, a `timedelta`, a `Path`; and a memoized
   pure function, which is a cache of a constant rather than state
   (`PURE_CALLEES`, `PURE_CHAIN_CALLEES`, `PURE_REPO_CALLEES`, `ALLOWED`).
"""

from __future__ import annotations

from pathlib import Path

from meizel import allowances
from meizel.state.ambient_state.roots import package_roots

_REPO_ROOT = Path(__file__).resolve().parents[3]
_POLICY = allowances.load(_REPO_ROOT)
_RULE = "ambient-state"

# ── 1. write-only facades ──────────────────────────────────────────────────

# A handle to a logger / meter / tracer: written through, never read to decide.
SINK_CALLEES = frozenset(_POLICY.names(_RULE, "sink-callees"))
SINK_CALLEE_SUFFIXES = _POLICY.names(_RULE, "sink-callee-suffixes")

# Files that ARE a sink facade (path -> why): the module-level state in them is the facade's own
# write-only machinery, so none of it is reported. The exemption covers state only: a thread or
# task a facade starts is still reported.
SINK_FACADES: dict[str, str] = _POLICY.reasons(_RULE, "sink-facades")

# ── 2. framework wiring ────────────────────────────────────────────────────

# The application / route table / CLI definition, built once, and the synchronization
# primitives: the lock is wiring, the state it guards is not.
WIRING_CALLEES = frozenset(_POLICY.names(_RULE, "wiring-callees"))

# ── 3. pure constants ──────────────────────────────────────────────────────

# Constructors whose result is fixed by their literal arguments.
PURE_CALLEES = frozenset(_POLICY.names(_RULE, "pure-callees"))

# Constructors that are pure only when a method is called on the result at once
# (`hashlib.sha256(b"").hexdigest()`): assigned bare they would be a stateful hasher.
PURE_CHAIN_CALLEES = frozenset(_POLICY.names(_RULE, "pure-chain-callees"))

# Repo functions that return a constant derived from literals only (callee -> why).
# A listed function that no longer exists fails as stale.
PURE_REPO_CALLEES: dict[str, str] = _POLICY.reasons(_RULE, "pure-repo-callees")

# Exact sites let through, `path::rule:name` (the baseline's own key shape) -> why.
# Used for a memoized pure function: `lru_cache` over a derivation that depends on
# nothing mutable is a cache of a constant, not state. A site that stops being
# reported fails as stale.
ALLOWED: dict[str, str] = _POLICY.reasons(_RULE, "sites")

# ── 4. slice-governed packages ─────────────────────────────────────────────

# Packages whose configuration arrives as constructor-injected slices: package dir ->
# its composition-root modules, the only modules there that may read `settings`
# (meizel/state/ambient_state/sliced.py). A package declares itself in its own
# `ambient_roots.toml` (`settings = [...]`, see roots.py) when it is sliced; leaving needs
# a decision record. A declared root that no longer exists fails as stale.
SLICED_PACKAGES: dict[str, frozenset[str]] = package_roots(_REPO_ROOT, "settings")

# ── 5. database-handle packages ────────────────────────────────────────────

# Packages that take a `Database` from their composition root and dial nothing ambiently:
# package dir -> the modules that may call `Database.from_settings()` (scripts/structure/
# ambient_state/dbhandle.py). A package declares `db = [...]` in its own `ambient_roots.toml`
# (see roots.py) in the change that migrates its last ambient dial; the declaration is the lock.
# A declared root that no longer exists fails as stale.
DB_HANDLE_PACKAGES: dict[str, frozenset[str]] = package_roots(_REPO_ROOT, "db")

# ── 6. endpoint-table packages ─────────────────────────────────────────────

# Packages that take their daemon's `ServiceEndpoint` (or the `ServiceEndpoints` table) from
# their composition root: package dir -> the modules that may call
# `ServiceEndpoints.from_settings()` (meizel/state/ambient_state/endpointrule.py). A package
# declares `endpoints = [...]` in its own `ambient_roots.toml` (see roots.py); a declared root
# that no longer exists fails as stale.
ENDPOINT_PACKAGES: dict[str, frozenset[str]] = package_roots(_REPO_ROOT, "endpoints")

# ── 7. event-bus packages ───────────────────────────────────────────────────

# Packages that take an `EventBus` handle from their composition root: package dir -> the
# modules that may call `EventBus.from_settings()` (meizel/state/ambient_state/busrule.py).
# A package declares `bus = [...]` in its own `ambient_roots.toml` (see roots.py); a declared
# root that no longer exists fails as stale.
BUS_PACKAGES: dict[str, frozenset[str]] = package_roots(_REPO_ROOT, "bus")

# ── 8. clock packages ────────────────────────────────────────────────────────

# Packages that take a `Clock` from their composition root: package dir -> the modules that may
# call `Clock.from_settings()` (meizel/state/ambient_state/clockrule.py). A package declares
# `clock = [...]` in its own `ambient_roots.toml` (see roots.py); a declared root that no longer
# exists fails as stale.
CLOCK_PACKAGES: dict[str, frozenset[str]] = package_roots(_REPO_ROOT, "clock")
