"""Provider plugin contract — what a plugin's ``provider.py`` declares.

A provider plugin makes one more vendor's models *nameable*. It never decides
which model an agent runs on — no routing or provider-selected fallback
(``base/lm/docs/model-providers-as-plugins.md``,
``docs/decisions/engineering/design/simplification/2026-07-29-no-runtime-model-routing.md``). A ``provider.py`` registers nothing: it
exports ``contribute()`` returning a ``PluginContributions`` whose ``providers`` hold one
``ProviderContribution`` each (the binding, the model rows and the prices). The process's model
catalog (``base/lm/catalog/__init__.py``) is built by ``base/lm/plugin_providers.py``, which loads every
enabled plugin's ``provider.py`` once per process, before the first build / spawn validation /
model list, and installs each declaration into the catalog builder; the prefix map is flat — a
duplicate prefix, or one that nests inside another (``foo-`` vs ``foo-bar-``), fails fast at
installation.
Core registers no providers; enabled plugins are the sole source of bindings,
chat-model rows, provider vocabularies, media fallbacks, keys, and live prices.

A ``provider.py`` module may import ``base`` and LangChain packages only —
never ``ava`` / ``agent`` (the gateway, the labeler daemon, and the eval
harness load it, and none of those processes has an agent runtime). Plugin
discovery is keyed on ``plugin.py``, so a provider plugin ships one even when
it contributes nothing agent-side (an empty stub satisfies discovery and the
enable switch).

``ProviderBinding.key_env`` declares the key's `.env` delivery channel: the
gateway reads the file at spawn validation, bootstrap relays enabled bindings'
present keys to split runners, and every agent process and exec child reads the key
from its own boot of that channel. Plugin config images do not carry provider secrets.

Builder contract (plain Python, documented rather than schema'd — see
``docs/decisions/extensions/plugins/2026-07-19-plugin-core-boundary-wrapper-extension.md``):

- ``build(ctx)`` returns a ``BaseChatModel`` with no tools bound (the caller
  binds them). It is a pure function of ``ctx`` — no caller, agent, error
  history, or budget is visible.
- Missing API key raises ``RuntimeError`` immediately (``require_key``) — a
  clear build-time error, not a server 401 mid-turn.
- ``ctx.resolved_effort`` keeps the provider's established wire semantics.
  Builders with a constrained vocabulary validate with
  ``base.lm.effort.validate_effort``. Supported grades pass through verbatim;
  unsupported grades fail instead of remapping.
- ``thinking={"type": "disabled"}`` is honored per provider capability
  (mirror onto the local switch, or log-and-ignore like the Moonshot plugin)
  — the core dispatch resolves the cross-provider knobs first;
  the builder owns only the wire shape.
- A builder wrapping ``ChatAnthropic`` must set ``anthropic_protocol=True``
  so registration validates ``max_output_tokens`` on every spawnable model
  (langchain-anthropic falls back to a legacy 4096 for unknown ids — #169).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from base.lm.catalog.provider_contract import (
    PROVIDER_API_VERSION as PROVIDER_API_VERSION,
)
from base.lm.catalog.provider_contract import (
    AttachPolicy as AttachPolicy,
)
from base.lm.catalog.provider_contract import (
    BuildContext as BuildContext,
)
from base.lm.catalog.provider_contract import (
    InferenceSpeed as InferenceSpeed,
)
from base.lm.catalog.provider_contract import (
    PricePeriod as PricePeriod,
)
from base.lm.catalog.provider_contract import (
    PriceRates as PriceRates,
)
from base.lm.catalog.provider_contract import (
    PriceTier as PriceTier,
)
from base.lm.catalog.provider_contract import (
    PriceWindow as PriceWindow,
)
from base.lm.catalog.provider_contract import (
    ProviderBinding as ProviderBinding,
)
from base.lm.catalog.provider_contract import (
    ProviderContribution as ProviderContribution,
)
from base.lm.catalog.provider_contract import (
    ProviderRegistrationError as ProviderRegistrationError,
)
from base.lm.catalog.provider_contract import (
    ThinkingConfig as ThinkingConfig,
)
from base.lm.registry import ReferenceTps


def provider_key_present(key_env: str) -> bool:
    """Whether the live process env carries ``key_env``.

    The env half of the spawn-boundary channel (``_ensure_provider_key``):
    a pure agent-runner receives provider keys from ``/api/bootstrap``
    injected into os.environ (no materialized .env cluster facts since
    2026-08-01); the gateway loads its own ``.env`` into the process env at
    boot. Callers combine this with the ``.env``-file fallback.
    """
    import os

    return bool(os.environ.get(key_env))


def require_key(key_env: str) -> str:
    """Read a plugin provider's API key from the process environment, failing
    fast with the same posture as the core builders.

    The spawn-boundary check (``_ensure_provider_key``) reads os.environ
    first, then the unit's ``.env`` file fallback; a pure agent-runner
    receives the key through ``/api/bootstrap`` (plugin-secrets section) into
    ``os.environ`` before any build. This helper is the build-time half of
    the same channel.
    """
    import os

    key = os.environ.get(key_env)
    if not key:
        raise RuntimeError(
            f"{key_env} not set — this provider needs its key; "
            "configure in ~/.ava/.env or export before starting"
        )
    return key


def with_fast_variants(
    provider: ProviderContribution,
    prices: Mapping[str, PriceRates],
    *,
    reference_tps: Mapping[str, ReferenceTps] | None = None,
) -> ProviderContribution:
    """Derive separately priced ``<model>-fast`` services from standard rows.

    ``prices`` names standard IDs with vendor-confirmed Fast rates. Model facts
    and tuning stay owned by the standard row; Fast replacements follow only
    other declared Fast services. Provider builders translate ``fast_of`` into
    the wire model and their own Fast parameter.
    """
    if provider.binding.served_speed is None:
        raise ValueError("Fast services require a provider served_speed adapter")
    models = dict(provider.models)
    pricing = dict(provider.pricing)
    throughput = reference_tps or {}
    if not throughput.keys() <= prices.keys():
        raise ValueError("Fast TPS references must name declared Fast services")
    for standard_id, price in prices.items():
        standard = provider.models[standard_id]
        fast_id = f"{standard_id}-fast"
        if standard.fast_of is not None or fast_id in models or fast_id in pricing:
            raise ValueError(f"Invalid or duplicate Fast service {fast_id!r}")
        replacement = standard.superseded_by
        models[fast_id] = replace(
            standard,
            fast_of=standard_id,
            reference_tps=throughput.get(standard_id),
            superseded_by=f"{replacement}-fast" if replacement in prices else None,
        )
        pricing[fast_id] = price
    return replace(provider, models=models, pricing=pricing)
