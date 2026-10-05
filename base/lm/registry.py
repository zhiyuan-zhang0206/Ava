"""Per-model facts and tunable defaults: `ModelSpec`, `ModelTuning` and their resolution.

Every per-model fact and per-model tunable default lives in one ``ModelSpec`` per model id; the
table of them is ``ModelCatalog.models`` (``base/lm/catalog.py``), built once per process by the
provider loader (``base/lm/plugin_providers.py:model_catalog``). Core registers no provider or
model rows: provider plugins are the sole source of chat ``ModelSpec`` entries, per-provider
bindings, and complete runtime price lattices. ``pricing_catalog_archive.json`` is the reviewed
reconciliation ledger and catalog-only source; selection lives in ``base.lm.pricing``.

## Config layering — how a per-model default takes effect

Everything tunable is per-model by default, with a shared fallback::

    code shared default            (DEFAULT_TUNING — the fully-populated floor)
    < per-model default            (the model's ModelSpec.tuning — code table, None = no opinion)
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

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from typing import Any

from base.host.env.agent_slices import ModelOverrides
from base.lm.pricing import PriceBook

# ---------------------------------------------------------------------------
# ModelTuning — per-model DEFAULTS for settings fields
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelTuning:
    """Per-model defaults for per-model-defaultable settings fields.

    Field names MATCH the flat config field names (``base.config``) exactly —
    ``resolve_setting`` maps between the two by name, and
    ``base/lm/tests/test_model_registry.py`` asserts the alignment. ``None``
    means "no per-model opinion; fall through to ``DEFAULT_TUNING``".

    Communication-style / prompt-section values are the per-model *behavior
    profile*: which guidance sections a model family gets and how chatty it
    should be. Mechanical values (compact fractions, retry, stream timeouts)
    tune the runtime around each model's failure modes.
    """

    # -- mechanical --
    auto_compact_fraction: float | None = None
    auto_compact_ceiling_tokens: int | None = None
    compact_reminder_fraction: float | None = None
    reasoning_effort: str | None = None
    claude_thinking_budget_tokens: int | None = None
    llm_retry_max_attempts: int | None = None
    llm_stream_ttft_timeout_seconds: float | None = None
    llm_stream_total_timeout_seconds: float | None = None
    llm_stream_inter_chunk_timeout_seconds: float | None = None
    # -- prompt behavior --
    agent_communication_style: str | None = None
    prompt_user_tone_enabled: bool | None = None
    prompt_prefer_sdk_enabled: bool | None = None
    prompt_keep_it_simple_enabled: bool | None = None
    prompt_output_conciseness_enabled: bool | None = None
    prompt_ui_delivery_enabled: bool | None = None
    prompt_outcome_reporting_enabled: bool | None = None
    prompt_action_caution_enabled: bool | None = None
    prompt_align_before_action_enabled: bool | None = None
    prompt_delegation_check_enabled: bool | None = None
    prompt_capabilities_match_first_enabled: bool | None = None
    prompt_cross_machine_delegation_enabled: bool | None = None
    prompt_file_driven_work_enabled: bool | None = None
    prompt_temporal_awareness_enabled: bool | None = None
    prompt_invest_future_enabled: bool | None = None
    prompt_memory_behavior_enabled: bool | None = None


# The shared-default floor: the values every model gets when neither the model
# entry nor the user says otherwise. These are the former pydantic Field
# defaults of the corresponding settings fields (moved here when those fields
# became None-sentinel). Every field must be non-None — asserted at import.
DEFAULT_TUNING = ModelTuning(
    # One flat rule for the whole roster: force-compact at 40% of the model's own
    # context window, remind at 30%. Both are fractions of `context_window`, so
    # "the same rule" means different absolute token counts per model, and a model
    # added to the registry inherits it with no entry of its own. The deepseek
    # entries are the roster's exception: user decision (2026-08-29) pins them at
    # soft 374k / hard 512k (0.374 / 0.512 of their 1M window).
    auto_compact_fraction=0.4,
    auto_compact_ceiling_tokens=0,  # 0 = no absolute cap; the fraction alone decides
    compact_reminder_fraction=0.3,
    # "" = "no per-model opinion" — the floor for models WITHOUT a pinned
    # tuning value. Spawnable models must pin a concrete value (validated
    # below): the spawn picker pre-selects each model's default effort, and ""
    # is not displayable as a default. An explicit AVA_REASONING_EFFORT=""
    # still pins "provider's own default" at the config layer.
    reasoning_effort="",  # "" = each provider's own default effort
    claude_thinking_budget_tokens=0,  # 0 = leave extended thinking off
    llm_retry_max_attempts=6,
    llm_stream_ttft_timeout_seconds=30.0,
    # Hard wall for one streaming attempt. Gap timeouts still catch silent
    # providers sooner; this ceiling catches a response that drip-feeds forever.
    llm_stream_total_timeout_seconds=3600.0,
    # 300, not 10: Claude Code (API_FORCE_IDLE_TIMEOUT, documented as a 5-minute
    # idle timeout) and Codex CLI (stream_idle_timeout_ms) independently ship 300
    # for this exact parameter. Long mid-stream silence is documented-normal, not
    # a fault — Anthropic's default `display: "omitted"` thinking emits NO
    # thinking_delta at all, and the streaming docs warn of tool-call gaps. At 10
    # this timeout was manufacturing turn aborts out of healthy streams.
    llm_stream_inter_chunk_timeout_seconds=300.0,
    # User ruling (2026-08-22): narration guidance is off by default for every
    # model; the section is omitted unless an explicit value opts in.
    agent_communication_style="off",
    # User decision (2026-09-03): tone guidance is on by default; Claude models opt out.
    prompt_user_tone_enabled=True,
    prompt_prefer_sdk_enabled=True,
    prompt_keep_it_simple_enabled=True,
    prompt_output_conciseness_enabled=True,
    prompt_ui_delivery_enabled=True,
    prompt_outcome_reporting_enabled=True,
    prompt_action_caution_enabled=True,
    prompt_align_before_action_enabled=True,
    prompt_delegation_check_enabled=True,
    prompt_capabilities_match_first_enabled=True,
    prompt_cross_machine_delegation_enabled=True,
    prompt_file_driven_work_enabled=True,
    prompt_temporal_awareness_enabled=True,
    prompt_invest_future_enabled=True,
    prompt_memory_behavior_enabled=True,
)


# ---------------------------------------------------------------------------
# ModelSpec — per-model facts + the tuning defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    """Everything the framework knows about one concrete model id.

    Facts (windows, caps, effort vocabulary) drive the factory, the
    compact machinery, and the UI catalog; ``tuning`` carries the per-model
    settings defaults resolved by ``resolve_setting``. A fact left ``None``
    means "unknown / not applicable" and keeps the model out of the
    corresponding derived view — exactly the membership the retired parallel
    tables had.
    """

    provider: str  # supported-models group key == build_chat_model prefix
    spawnable: bool = False  # offered in the frontend spawn dropdown
    unavailable_fallback: str | None = None  # temporarily withdraw this id from new selections
    # while preserving existing configurations: build_chat_model resolves the
    # explicitly named fallback. The target must be a registered, spawnable
    # model; no provider error is silently retried as a fallback.
    superseded_by: str | None = None  # the model id that replaced this one in the
    # spawn picker. Purely a display fact: a superseded model stays spawnable
    # (and therefore fully config-valid — spawn/restart validation and the
    # settings panels accept it unchanged), the picker just hides it by default
    # in favor of the replacement. The replacement chain is set explicitly at
    # onboarding time (see the model-switch playbook), never inferred at runtime.
    context_window: int | None = None  # max input tokens (compact thresholds derive from it)
    max_output_tokens: int | None = None  # documented output cap; pinned explicitly on the
    # anthropic-protocol branches (claude/deepseek) — langchain-anthropic falls back to a
    # legacy 4096 default for unknown ids, truncating thinking mid-turn (#169)
    knowledge_cutoff: str | None = None  # YYYY-MM, appended to the system prompt
    model_identity: str | None = (
        None  # per-model identity note, injected before cutoff in system prompt
    )
    effort_levels: tuple[str, ...] | None = None  # the effort vocabulary this model's knob
    # accepts (wire `output_config.effort` levels for adaptive claude; the binary
    # thinking on/off vocabulary for extended-thinking-only models; the provider's
    # clamp vocabulary elsewhere). Serves the spawn-dialog dropdown + the claude clamp.
    extended_thinking_only: bool = False  # claude models whose only thinking mode is manual
    # extended thinking (budget_tokens; default OFF) and that 400 on `effort`
    thinking_always_on: bool = False  # models whose reasoning cannot be switched off: a
    # caller disabling thinking gets a warning instead of a wire body the endpoint
    # rejects (kimi-k3; glm-5.3 / glm-5.3-flash, whose thinking.type=disabled 400s —
    # verified live 2026-08-27, error code 1210)
    streaming: bool = True  # construction-time streaming default; False only where a
    # model's streaming path is known-worse than its non-streaming one
    media_types: frozenset[str] = frozenset()
    """Media types the model's provider binding accepts natively. Members are
    "image" / "pdf" / "audio" / "video". Empty (default) = text-only. Supersedes
    the per-model `vision` bool (Task #1342): one field carries the whole capability
    matrix, and `model_supports_vision` derives from it."""
    attach_modalities: frozenset[str] | None = None
    """The media types `ava.self.attach` accepts for this model, when attach is
    more restrictive than the model's native `media_types` (user ruling
    2026-08-28). Attach registers local files into the same message pipeline,
    so the native matrix is the default contract — None means "no attach-specific
    opinion; follow `media_types`". Empty frozenset = attach is unavailable even
    though the endpoint could receive media. A declared set must be a subset of
    `media_types` (enforced by `validate_spec`): a model cannot attach a
    modality its endpoint cannot receive."""
    tuning: ModelTuning = field(default_factory=ModelTuning)


def media_types_for_model(model: str) -> frozenset[str]:
    """Native media capability for `model` across the three provider tiers.

    Registered plugin models use their per-model ``ModelSpec.media_types``. An
    unregistered id under a plugin prefix gets the binding's v1 image-only
    ``vision`` capability. No match means text-only.
    """
    from base.lm.plugin_providers import model_catalog  # the catalog module imports this one

    catalog = model_catalog()
    spec = catalog.models.get(model)
    if spec is not None:
        return spec.media_types
    for prefix, binding in catalog.bindings.items():
        if model.startswith(prefix):
            return frozenset({"image"}) if binding.vision else frozenset()
    return frozenset()


def attach_modalities_for_model(model: str) -> frozenset[str]:
    """The media types `ava.self.attach` accepts for `model`.

    `ModelSpec.attach_modalities` when the entry declares an attach-specific
    opinion; otherwise the model's native `media_types` — attach registers
    files into the same message pipeline, so the native matrix is the default
    contract (user ruling 2026-08-28). Empty result = text-only for attach:
    no files can be registered, the SDK docs drop the member, and the call
    raises. Unregistered ids fall through the same provider tiers as
    `media_types_for_model`.

    Raw-entry semantics: a withdrawn id answers with its declared set; the
    registration gates resolve the effective model first (task #3212)."""
    from base.lm.plugin_providers import model_catalog  # the catalog module imports this one

    spec = model_catalog().models.get(model)
    if spec is not None:
        if spec.attach_modalities is not None:
            return spec.attach_modalities
        return spec.media_types
    return media_types_for_model(model)


def validate_spec(
    model_id: str,
    spec: ModelSpec,
    *,
    anthropic_protocol: bool,
    prices: PriceBook,
    pending_price_models: Collection[str] = (),
) -> None:
    """Fail fast on a registry gap for one spawnable model entry.

    A spawnable model missing a required fact would surface as a degraded UI row /
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
    if model_id not in pending_price_models and prices.rates_at(model_id, input_tokens=0) is None:
        raise RuntimeError(
            f"spawnable model {model_id!r} has no current price — a catalog-priced "
            "model needs an archive entry; a plugin model needs a price in its "
            "register() call"
        )
    # The spawn picker pre-selects each model's default effort
    # (GET /api/models reasoning_effort_default) — without a concrete
    # per-model value it cannot show one ("" means "provider's own
    # default", which is not a displayable rung), and the UI would regress
    # to a synthetic "Effort: default" option. Pin a real default (the
    # vendor's documented one; see docs/decisions/2026-07-25-per-model-
    # tuning-values.md Decision 4).
    if not spec.tuning.reasoning_effort:
        raise RuntimeError(
            f"spawnable model {model_id!r} has no concrete reasoning_effort default — "
            f"pin one in its ModelTuning ('' provider-default is not displayable "
            f"in the spawn picker)"
        )
    # The spawn picker renders one rung per effort_levels entry and
    # pre-selects the default — a default outside the ladder would render no
    # selected rung while a different effort goes on the wire, the same
    # what-you-see != what-is-sent class as the missing-default guard above.
    levels = spec.effort_levels or ()
    if spec.tuning.reasoning_effort not in levels:
        raise RuntimeError(
            f"spawnable model {model_id!r} has reasoning_effort default "
            f"{spec.tuning.reasoning_effort!r} outside its effort_levels {levels!r} — "
            f"pin the default to one of the model's own rungs (the spawn picker "
            f"cannot render it otherwise)"
        )
    if anthropic_protocol and spec.max_output_tokens is None:
        raise RuntimeError(
            f"spawnable model {model_id!r} needs max_output_tokens — "
            f"the anthropic-protocol bindings must pin the output cap explicitly"
        )


def validate_models(
    models: Mapping[str, ModelSpec],
    *,
    prices: PriceBook,
    anthropic_protocol_by_model: Mapping[str, bool] | None = None,
) -> None:
    """Validate shared defaults and the complete registered model graph."""
    for tuning_field in dataclass_fields(ModelTuning):
        if getattr(DEFAULT_TUNING, tuning_field.name) is None:
            raise RuntimeError(
                f"DEFAULT_TUNING.{tuning_field.name} is None — the shared-default floor "
                f"must be fully populated (it is the last resort of resolve_setting)"
            )
    for model_id, spec in models.items():
        validate_spec(
            model_id,
            spec,
            anthropic_protocol=(
                False
                if anthropic_protocol_by_model is None
                else anthropic_protocol_by_model[model_id]
            ),
            prices=prices,
        )
    _validate_supersession_links(models)
    _validate_supersession_chains(models)
    _validate_unavailable_fallbacks(models)


def _validate_supersession_links(models: Mapping[str, ModelSpec]) -> None:
    # The supersession chain must stay coherent — a broken link would hide a
    # model from the picker while its replacement is absent or invisible.
    for model_id, spec in models.items():
        replacement_id = spec.superseded_by
        if replacement_id is None:
            continue
        if replacement_id == model_id:
            raise RuntimeError(
                f"model {model_id!r} lists itself as its own replacement — "
                "fix superseded_by in its provider plugin register() call"
            )
        if replacement_id not in models:
            raise RuntimeError(
                f"model {model_id!r} is superseded by {replacement_id!r}, which is "
                f"not in models — point superseded_by at a registered model id"
            )
        target = models[replacement_id]
        if not target.spawnable:
            raise RuntimeError(
                f"model {model_id!r} is superseded by {replacement_id!r}, which is "
                f"not spawnable — the replacement would never show in the picker"
            )


def _validate_supersession_chains(models: Mapping[str, ModelSpec]) -> None:
    # After every link is known-good, follow each chain to guarantee it ends
    # at a visible model instead of cycling through hidden models forever.
    for model_id, spec in models.items():
        seen = {model_id}
        replacement_id = spec.superseded_by
        while replacement_id is not None:
            if replacement_id in seen:
                raise RuntimeError(
                    f"superseded_by cycle from model {model_id!r} — "
                    f"point the chain at a visible model"
                )
            seen.add(replacement_id)
            replacement_id = models[replacement_id].superseded_by


def _validate_unavailable_fallbacks(models: Mapping[str, ModelSpec]) -> None:
    # A temporary withdrawal is an explicit routing decision, not a general
    # provider-error fallback. Keep both ends concrete so an existing config
    # can safely resolve to the model the picker offers instead.
    for model_id, spec in models.items():
        fallback_id = spec.unavailable_fallback
        if fallback_id is None:
            continue
        if spec.spawnable:
            raise RuntimeError(
                f"temporarily unavailable model {model_id!r} remains spawnable — "
                "remove it from the picker before assigning unavailable_fallback"
            )
        if fallback_id not in models:
            raise RuntimeError(
                f"temporarily unavailable model {model_id!r} falls back to {fallback_id!r}, "
                "which is not in models"
            )
        if not models[fallback_id].spawnable:
            raise RuntimeError(
                f"temporarily unavailable model {model_id!r} falls back to {fallback_id!r}, "
                "which is not spawnable"
            )


def resolve_available_model(model: str) -> str:
    """Resolve an explicitly withdrawn model id to its registered fallback.

    Unknown and currently available ids pass through. Registry validation keeps
    a fallback to one available hop, so no dynamic provider-error retry is
    hidden behind this resolution. Provider plugins are loaded first, so a
    plugin-declared withdrawal resolves on the first call of a fresh process
    too (task #3212).
    """
    from base.lm.plugin_providers import model_catalog  # the catalog module imports this one

    spec = model_catalog().models.get(model)
    return spec.unavailable_fallback if spec and spec.unavailable_fallback else model


def normalize_overlay_llm_model(config: dict[str, object]) -> tuple[str, str] | None:
    """Settle a withdrawn ``llm_model`` in a config overlay to its registered fallback.

    The write-side counterpart of `admit_stored_model`'s wake-time settlement:
    every overlay write (spawn row / self restart / ops restart) funnels through
    here so a withdrawn id never lands in ``agents_meta.config_overlay`` — the
    wake would normalize it later anyway, but the stored name is what forks,
    snapshots and readers copy (task #4306: the 9/20-21 recurrence wrote 12
    retired ids through the spawn path).

    Mutates ``config`` in place and returns ``(requested, resolved)`` when the
    name was rewritten, so the writer can emit the spawner-visible receipt — a
    silent rewrite would leave the stale template in place and keep producing
    withdrawn ids. Returns ``None`` when there is nothing to settle: no
    ``llm_model``, an available id, or an unknown id (unknown ids are the spawn
    boundary's validation concern — `validate_model_config`).
    """
    requested = config.get("llm_model")
    if not isinstance(requested, str):
        return None
    resolved = resolve_available_model(requested)
    if resolved == requested:
        return None
    config["llm_model"] = resolved
    return requested, resolved


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


_OVERRIDE_FIELDS = frozenset(f.name for f in dataclass_fields(ModelOverrides))


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
    from base.lm.plugin_providers import model_catalog  # the catalog module imports this one

    floor = getattr(DEFAULT_TUNING, setting)
    spec = model_catalog().models.get(model)
    tuned = getattr(spec.tuning, setting) if spec is not None else None
    if explicit is not None:
        return ResolvedSetting(setting, explicit, "explicit", floor, tuned, explicit)
    if tuned is not None:
        return ResolvedSetting(setting, tuned, "model-default", floor, tuned, None)
    return ResolvedSetting(setting, floor, "shared-default", floor, tuned, None)


def resolve_setting(setting: str, *, model: str, overrides: ModelOverrides | None = None) -> Any:
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
        overrides: the agent's explicit values (its slices' `overrides`). A
            value the agent set is the explicit layer; an unset one is the
            cluster default, as is every value when `overrides` is omitted, which
            is right only for a reader that is not serving one agent (a daemon).
    """
    from base.config import get_field

    # Membership check first: a non-tuning field must never resolve through this
    # path, even when it happens to carry an explicit value. Doing it here (not
    # only inside explain_setting) keeps the AttributeError ahead of the
    # get_field lookup, which would KeyError on a name that is no config field.
    getattr(DEFAULT_TUNING, setting)
    if overrides is not None and setting in _OVERRIDE_FIELDS:
        pinned = getattr(overrides, setting)
        if pinned is not None:
            return explain_setting(setting, model=model, explicit=pinned).value
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
