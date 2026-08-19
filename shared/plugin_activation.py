"""Injection-surface activation telemetry — what a plugin actually DID, as
opposed to what it registered.

`shared/plugin_contributions.py` is the registration ledger: it answers "which
plugin put this hook / wrap / prompt section here". It deliberately records
nothing about runtime. This module is its runtime half: one `plugin_activation`
event each time a registered injection surface actually fires, keyed by exactly
the `(plugin, surface, identifier)` triple the ledger stores — so a `Contribution`
row and the activation events for it join on the same three strings, with no
second identifier space to keep in sync.

Two consumers asked for it (issue #40):

- **Philosophy §6** — "a shim ... measures its own obsolescence (activation
  telemetry per model) — 'removable' as a gauge, not a vibe". Every event
  carries the model in force, so "did `ava_syntax_fix` still fire under model X
  last week" is a query, not an opinion.
- **Self-evolution attribution** — the weekly loop attributes a bad run to
  skills via `skill_invoked`. Hooks and wraps used to run silently, so a plugin
  that rewrote state or short-circuited an SDK call in a bad run left no trace
  in the dataset. These events give it the plugin-side equivalent.

**Surface coverage** mirrors the issue's scoping decisions:

| surface | records when | identifier |
|---|---|---|
| `hooks` | the hook returns a non-empty state update (a `None` / `{}` return is pure observation and stays free) | the hook point (`before_llm`, …) |
| `sdkWraps` | the wrapper does not call `inner` exactly once — a short-circuit or a retry, i.e. the wrap changed control flow | the wrap target (`files.read`) |
| `systemPromptSections` | the contributor returns a non-empty section at prompt-build time (spawn / compact only — no per-turn cost) | the section function name |
| `state` | — | covered by the `hooks` record: plugin state writes travel through hook returns, so a separate probe would double-count |
| `sdkNamespaces` | — | already metered as `sdk_call` by `agent/sdk_metering.py`; counting it here too would double-count |

**Only plugin registrations are recorded.** `plugin` is `None` for the
framework's own hooks and prompt sections (registered outside a `PluginContext`)
and for a test's direct `ava.extend.wrap`, and those record nothing — exactly the
gate `plugin_contributions.record` applies to the ledger, so the two stay
parallel by construction.

**Side-channel contract**, identical to `shared/sdk_telemetry.py`: a recording
failure is swallowed (`Exception` only, so cancel/timeout injection still
propagates) and never perturbs hook or wrap semantics — no state update is
changed, no result or exception is altered. No new table: the events land in the
unified stream `sdk_call` already uses, so `collect.py` and
`shared.metrics_aggregate` read them with no new plumbing.
"""

from __future__ import annotations

import contextlib

from shared.log import logger
from shared.plugin_contributions import SurfaceId

# Event name written to the unified stream for one activation.
PLUGIN_ACTIVATION_EVENT = "plugin_activation"


def record(
    plugin: str | None,
    surface: SurfaceId,
    identifier: str,
    *,
    detail: str = "",
) -> None:
    """Record one activation of `plugin`'s contribution at `surface`/`identifier`.

    `plugin` is the name the registration was attributed to; `None` means the
    registration came from the framework or a test rather than from a plugin
    import, and nothing is recorded. `surface` and `identifier` must be spelled
    the way the matching `plugin_contributions.Contribution` spells them — that
    is what makes the ledger and these events joinable.

    `detail` is one line of specifics about *this* firing (the state keys a hook
    wrote, how many times a wrapper called `inner`, a section's length) — free
    text for a reader, never parsed.

    Emitted per firing, not deduplicated: the surfaces recorded here fire at
    most a few times per turn, so the volume is bounded by turns and stays far
    under `sdk_call`'s, and real counts are a better obsolescence gauge than a
    per-run boolean.
    """
    if plugin is None:
        return
    # The suppression boundary sits here, at the call site's edge: `record` is
    # what a hook runner / wrap layer / prompt builder calls, and none of them
    # may fail because telemetry did. `Exception` only, so a cancel/timeout
    # injection still propagates.
    with contextlib.suppress(Exception):
        emit(plugin, surface, identifier, detail)


def emit(plugin: str, surface: SurfaceId, identifier: str, detail: str) -> None:
    """Write one `plugin_activation` event. Raises on a broken sink — `record`
    is the guarded entry point every production caller uses."""
    from shared.config import settings

    logger.bind(
        event=PLUGIN_ACTIVATION_EVENT,
        plugin=plugin,
        surface=surface,
        identifier=identifier,
        detail=detail,
        model=settings.lm.llm_model,
    ).info("plugin_activation")
