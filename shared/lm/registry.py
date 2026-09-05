"""Single per-model registry — every per-model fact and per-model tunable
default lives in one ``MODELS: dict[id, ModelSpec]`` table.

Replaces the parallel per-model-id tables that had accumulated across
``factory.py`` / ``_effort.py`` (``SUPPORTED_MODELS``,
``MODEL_CONTEXT_WINDOW``, ``MODEL_KNOWLEDGE_CUTOFF``, ``_MODEL_DEFAULT_STREAMING``,
``_CLAUDE_MAX_TOKENS``, ``_DEEPSEEK_MAX_TOKENS``, ``_CLAUDE_EFFORT_LEVELS``,
``_CLAUDE_EXTENDED_THINKING_ONLY``, ``_CLAUDE_EXTENDED_THINKING_EFFORT_LEVELS``)
— their membership had drifted apart because adding a model
meant editing up to a dozen dicts. Here a model is one entry; the legacy table
names survive as derived views (below) so existing import sites keep working.
Externally mutable prices live separately in ``pricing_catalog.json`` and are
selected through ``shared.lm.pricing``.
Per-PROVIDER tables (prefix → API key, wire effort vocabularies for the
OpenAI-style endpoints, vision prefixes) are *not* per-model facts and stay in
``factory.py`` / ``_effort.py``.

## Config layering — how a per-model default takes effect

Everything tunable is per-model by default, with a shared fallback::

    code shared default            (DEFAULT_TUNING — the fully-populated floor)
    < per-model default            (MODELS[id].tuning — code table, None = no opinion)
    < .env / env explicit value    (the user's deliberate global choice)
    < per-agent overlay            (spawn/restart config_overlay via set_field)

``resolve_setting`` implements the layering. The sentinel for "the user did not
explicitly choose" is the settings field's ``None`` default: per-model-defaultable
settings fields are typed ``T | None = None``, and their former pydantic defaults
moved into ``DEFAULT_TUNING`` here. A non-None settings value always means an
explicit choice — whether it came from ``.env``, an exported env var, a gateway
bootstrap payload that forwards a ``.env``-set value, or a per-agent overlay
(``set_field`` writes a non-None value onto the settings singleton).

Why not detect explicitness via ``model_fields_set``: a split agent-runner
receives its config injected into ``os.environ`` from the gateway's
``/api/bootstrap``, which serves *every* bootstrap field (defaults included) —
``model_fields_set`` would mark everything explicit on a runner but not on a
single box, making the layering topology-dependent. The None-sentinel travels
as data through every distribution path (an unset field serializes as absent,
``bootstrap_config_values`` skips None), so the layering is uniform.

Per-model values are a CODE table on purpose — not a config dimension. The
``model_overrides`` override-store shape was retired by migration 0047; the
"one value lives in exactly one place" invariant holds: a per-model default is
code, an explicit user choice is ``.env``, a per-agent choice is the overlay.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from typing import Any

from shared.lm._model_registry_types import DEFAULT_TUNING, ModelSpec, ModelTuning
from shared.lm._model_specs_compatible import COMPATIBLE_MODELS
from shared.lm._model_specs_primary import PRIMARY_MODELS

MODELS: dict[str, ModelSpec] = {**PRIMARY_MODELS, **COMPATIBLE_MODELS}


# ---------------------------------------------------------------------------
# Derived views — the legacy table names, computed from MODELS
# ---------------------------------------------------------------------------

# Models offered in the frontend spawn dropdown + per-agent overlay, grouped by
# provider in registry order. Adding a model to MODELS with spawnable=True is
# the only edit needed for it to appear in the UI; provider availability still
# depends on the corresponding API key being set on the agent-runner.
# Rebuilt in place so plugin registration remains visible to imported readers.
SUPPORTED_MODELS: dict[str, list[str]] = {}

# Model context window sizes (max input tokens). Used by the token-usage
# endpoint and the compact machinery; a model without a known window is absent
# (the frontend hides the max segment).
MODEL_CONTEXT_WINDOW: dict[str, int] = {}

# Knowledge cutoff dates (YYYY-MM), appended to the system prompt so the agent
# knows the temporal boundary of its training data. A model without one simply
# gets no cutoff line.
MODEL_KNOWLEDGE_CUTOFF: dict[str, str] = {}

# Model identity — a per-model note injected before the knowledge cutoff
# in the system prompt so the model knows what it is running on.
MODEL_IDENTITY: dict[str, str] = {}


def _rebuild_derived_views() -> None:
    """Recompute the derived views from MODELS, in place.

    The views are module-level names imported across ~36 files; mutating the
    same dict objects (clear + update) keeps every existing import site
    working unchanged after a plugin registration.
    """
    SUPPORTED_MODELS.clear()
    for _model_id, _spec in MODELS.items():
        if _spec.spawnable:
            SUPPORTED_MODELS.setdefault(_spec.provider, []).append(_model_id)

    MODEL_CONTEXT_WINDOW.clear()
    MODEL_CONTEXT_WINDOW.update(
        {
            _model_id: _spec.context_window
            for _model_id, _spec in MODELS.items()
            if _spec.context_window is not None
        }
    )
    MODEL_KNOWLEDGE_CUTOFF.clear()
    MODEL_KNOWLEDGE_CUTOFF.update(
        {
            _model_id: _spec.knowledge_cutoff
            for _model_id, _spec in MODELS.items()
            if _spec.knowledge_cutoff is not None
        }
    )
    MODEL_IDENTITY.clear()
    MODEL_IDENTITY.update(
        {
            _model_id: _spec.model_identity
            for _model_id, _spec in MODELS.items()
            if _spec.model_identity is not None
        }
    )


def _validate_spec(model_id: str, spec: ModelSpec, *, anthropic_protocol: bool) -> None:
    """Fail fast on a registry gap for one spawnable model entry.

    A spawnable model missing a core fact would surface as a degraded UI row /
    an uncompactable agent / an unpriced eval — catch it where the entry is
    written instead. Shared by the import-time core validation and the
    registration-time plugin validation.
    """
    if spec.attach_modalities is not None and not spec.attach_modalities <= spec.media_types:
        raise RuntimeError(
            f"model {model_id!r} declares attach_modalities "
            f"{sorted(spec.attach_modalities)} that are not in its media_types "
            f"{sorted(spec.media_types)} — attach rides the same message "
            f"pipeline, so it cannot accept a modality the endpoint cannot receive"
        )
    if not spec.spawnable:
        return
    from shared.lm.pricing import rates_at

    missing = [
        fact
        for fact in ("context_window", "knowledge_cutoff", "effort_levels")
        if getattr(spec, fact) is None
    ]
    if missing:
        raise RuntimeError(
            f"spawnable model {model_id!r} is missing registry facts {missing} — "
            "fill them in its ModelSpec"
        )
    if rates_at(model_id, input_tokens=0) is None:
        raise RuntimeError(
            f"spawnable model {model_id!r} has no current price — a core model "
            "needs a shared/lm/pricing_catalog.json entry; a plugin model needs "
            "a price in its register() call"
        )
    # The spawn picker pre-selects each model's default effort
    # (GET /api/models reasoning_effort_default) — without a concrete
    # per-model value it cannot show one ("" means "provider's own
    # default", which is not a displayable rung), and the UI would regress
    # to a synthetic "Effort: default" option. Pin a real default (the
    # vendor's documented one; see decisions/2026-07-25-per-model-
    # tuning-values.md Decision 4).
    if not spec.tuning.reasoning_effort:
        raise RuntimeError(
            f"spawnable model {model_id!r} has no concrete reasoning_effort default — "
            f"pin one in its ModelTuning ('' provider-default is not displayable "
            f"in the spawn picker)"
        )
    if anthropic_protocol and spec.max_output_tokens is None:
        raise RuntimeError(
            f"spawnable model {model_id!r} needs max_output_tokens — "
            f"the anthropic-protocol bindings must pin the output cap explicitly"
        )


def register_models(
    provider: str,
    models: Mapping[str, ModelSpec],
    *,
    anthropic_protocol: bool = False,
) -> None:
    """Merge a plugin provider's ModelSpec entries into MODELS.

    Called by ``shared.lm/provider_api.py:register`` — not by core code.
    Mutates the same MODELS dict object (every imported reference sees it) and
    rebuilds the derived views in place; validates each new spawnable entry
    with the same facts/price/effort checks the core roster gets at import. A
    duplicate model id — core or another plugin's — is an error, never a
    precedence order.
    """
    for model_id, spec in models.items():
        if spec.provider != provider:
            raise ValueError(
                f"plugin model {model_id!r} declares provider {spec.provider!r}, "
                f"but it is registering under {provider!r} — fix ModelSpec.provider"
            )
        if model_id in MODELS:
            raise RuntimeError(
                f"model id {model_id!r} is already registered (core roster or an "
                "earlier plugin) — model ids are flat and a duplicate is an error"
            )
        _validate_spec(model_id, spec, anthropic_protocol=anthropic_protocol)
    MODELS.update(models)
    _rebuild_derived_views()


def _validate_registry() -> None:
    """Fail fast at import on a core-registry gap (see _validate_spec)."""
    for tuning_field in dataclass_fields(ModelTuning):
        if getattr(DEFAULT_TUNING, tuning_field.name) is None:
            raise RuntimeError(
                f"DEFAULT_TUNING.{tuning_field.name} is None — the shared-default floor "
                f"must be fully populated (it is the last resort of resolve_setting)"
            )
    for model_id, spec in MODELS.items():
        _validate_spec(
            model_id,
            spec,
            anthropic_protocol=spec.provider == "claude",
        )

    # The supersession chain must stay coherent — a broken link would hide a
    # model from the picker while its replacement is absent or invisible.
    for model_id, spec in MODELS.items():
        replacement_id = spec.superseded_by
        if replacement_id is None:
            continue
        if replacement_id == model_id:
            raise RuntimeError(
                f"model {model_id!r} lists itself as its own replacement — "
                f"fix superseded_by in shared/lm/registry.py:MODELS"
            )
        if replacement_id not in MODELS:
            raise RuntimeError(
                f"model {model_id!r} is superseded by {replacement_id!r}, which is "
                f"not in MODELS — point superseded_by at a registered model id"
            )
        target = MODELS[replacement_id]
        if not target.spawnable:
            raise RuntimeError(
                f"model {model_id!r} is superseded by {replacement_id!r}, which is "
                f"not spawnable — the replacement would never show in the picker"
            )

    # After every link is known-good, follow each chain to guarantee it ends
    # at a visible model instead of cycling through hidden models forever.
    for model_id, spec in MODELS.items():
        seen = {model_id}
        replacement_id = spec.superseded_by
        while replacement_id is not None:
            if replacement_id in seen:
                raise RuntimeError(
                    f"superseded_by cycle from model {model_id!r} — "
                    f"point the chain at a visible model"
                )
            seen.add(replacement_id)
            replacement_id = MODELS[replacement_id].superseded_by

    # A temporary withdrawal is an explicit routing decision, not a general
    # provider-error fallback. Keep both ends concrete so an existing config
    # can safely resolve to the model the picker offers instead.
    for model_id, spec in MODELS.items():
        fallback_id = spec.unavailable_fallback
        if fallback_id is None:
            continue
        if spec.spawnable:
            raise RuntimeError(
                f"temporarily unavailable model {model_id!r} remains spawnable — "
                "remove it from the picker before assigning unavailable_fallback"
            )
        if fallback_id not in MODELS:
            raise RuntimeError(
                f"temporarily unavailable model {model_id!r} falls back to {fallback_id!r}, "
                "which is not in MODELS"
            )
        if not MODELS[fallback_id].spawnable:
            raise RuntimeError(
                f"temporarily unavailable model {model_id!r} falls back to {fallback_id!r}, "
                "which is not spawnable"
            )


_rebuild_derived_views()
_validate_registry()


def resolve_available_model(model: str) -> str:
    """Resolve an explicitly withdrawn model id to its registered fallback.

    Unknown and currently available ids pass through. Registry validation keeps
    a fallback to one available hop, so no dynamic provider-error retry is
    hidden behind this resolution.
    """
    spec = MODELS.get(model)
    return spec.unavailable_fallback if spec and spec.unavailable_fallback else model


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedSetting:
    """One setting's effective value plus the layer that produced it.

    The introspectable form of ``resolve_setting``: same layering, but it names
    the winning layer and keeps every candidate, so an operator can see WHY a
    model runs with the value it does instead of only what the value is.
    """

    setting: str
    value: Any  # the effective value — what resolve_setting returns
    source: str  # "explicit" | "model-default" | "shared-default"
    shared_default: Any  # the DEFAULT_TUNING floor; always present
    model_default: Any | None  # this model's own opinion (None = none / unregistered)
    explicit_value: Any | None  # the user's pinned value (None = not pinned)


def tuning_field_names() -> tuple[str, ...]:
    """Every per-model-defaultable settings field name, in ``ModelTuning`` order.

    The exact set of fields ``resolve_setting`` governs — what a per-model view
    enumerates, so adding a tunable to ``ModelTuning`` surfaces it with no
    second list to keep in sync.
    """
    return tuple(f.name for f in dataclass_fields(ModelTuning))


def explain_setting(setting: str, *, model: str, explicit: Any) -> ResolvedSetting:
    """``resolve_setting``'s layering, with the winning layer named.

    The explicit value is an ARGUMENT rather than read here: the runtime passes
    the in-process settings value, while the config panel passes the value read
    fresh from `.env` (what the next agent process will boot with). One layering
    implementation for both, so the displayed resolution cannot drift from the
    one the agent actually gets.
    """
    floor = getattr(DEFAULT_TUNING, setting)
    spec = MODELS.get(model)
    tuned = getattr(spec.tuning, setting) if spec is not None else None
    if explicit is not None:
        return ResolvedSetting(setting, explicit, "explicit", floor, tuned, explicit)
    if tuned is not None:
        return ResolvedSetting(setting, tuned, "model-default", floor, tuned, None)
    return ResolvedSetting(setting, floor, "shared-default", floor, tuned, None)


def resolve_setting(setting: str, *, model: str) -> Any:
    """The effective value of a per-model-defaultable settings field for `model`.

    Layering (weakest first): ``DEFAULT_TUNING`` shared default < the model's
    ``tuning`` entry < an explicit settings value. The settings value is
    explicit exactly when it is non-None — these fields are ``T | None = None``
    and every real source (.env, exported env, bootstrap-forwarded env, the
    per-agent config overlay) writes a non-None value.

    Args:
        setting: flat config field name; must be a ``ModelTuning`` field
            (AttributeError otherwise — a typo fails fast).
        model: the model id whose per-model default applies. An unregistered
            model simply has no per-model layer.
    """
    from shared.config import get_field

    # Membership check first: a non-tuning field must never resolve through this
    # path, even when it happens to carry an explicit value. Doing it here (not
    # only inside explain_setting) keeps the AttributeError ahead of the
    # get_field lookup, which would KeyError on a name that is no config field.
    getattr(DEFAULT_TUNING, setting)
    try:
        explicit = get_field(setting)
    except AttributeError:
        # The field's owning domain is not constructed in this process's
        # profile (Task #944): the tuning fields live in the AGENT domain, and
        # the gateway's token-usage / context-breakdown display endpoints call
        # resolve_context_budget too. Fail-fast is right for a typo, but this
        # is a legal cross-profile read — degrade to the registry floor and
        # let the agent process itself keep reading the explicit value.
        explicit = None
    return explain_setting(setting, model=model, explicit=explicit).value
