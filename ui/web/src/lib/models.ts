// Shared helpers for the two GET /api/models consumers (spawn-button's
// picker + the Control > Display "Model picker" section) — both render the
// provider-grouped model list, one as a selectable dropdown, the other as
// per-model visibility toggles.

import type { ModelsResponse } from "./types";

// Display label for a provider key. `ModelsResponse.providers` is the
// source of truth for *which* providers/models exist and their order
// (gateway/routers/agents.py:get_models, backed by
// shared/lm/factory.py:SUPPORTED_MODELS) — this map only prettifies the
// label for providers we know about. An unrecognized provider (added
// server-side before this map catches up) still renders, just capitalized
// instead of using its conventional styling.
const PROVIDER_LABELS: Record<string, string> = {
  deepseek: "DeepSeek",
  claude: "Claude",
  gemini: "Gemini",
  gpt: "GPT",
  mimo: "MiMo",
  kimi: "Kimi",
  glm: "GLM",
  qwen: "Qwen",
};

export function providerLabel(provider: string): string {
  return (
    PROVIDER_LABELS[provider] ??
    (provider.length > 0 ? provider[0].toUpperCase() + provider.slice(1) : provider)
  );
}

/**
 * Models grouped in API provider order, with each provider's models ordered
 * by cache-miss input price descending (most expensive first). An optional
 * predicate filters individual models before sorting (e.g. the spawn picker
 * excludes user-hidden models); a provider left with no models after filtering
 * is dropped rather than rendered empty.
 */
export function groupedModels(
  modelsData: ModelsResponse | undefined,
  filter?: (model: string) => boolean,
): [provider: string, models: string[]][] {
  if (!modelsData) return [];
  const modelInfoByName: Record<
    string,
    ModelsResponse["models"][string] | undefined
  > = modelsData.models;
  return Object.entries(modelsData.providers)
    .map(([provider, models]): [string, string[]] => {
      const filteredModels = filter ? models.filter(filter) : [...models];
      filteredModels.sort((left, right) => {
        const leftPrice = modelInfoByName[left]?.pricing?.input;
        const rightPrice = modelInfoByName[right]?.pricing?.input;
        if (leftPrice === undefined) return rightPrice === undefined ? 0 : 1;
        if (rightPrice === undefined) return -1;
        return rightPrice - leftPrice;
      });
      return [provider, filteredModels];
    })
    .filter(([, models]) => models.length > 0);
}

/**
 * Whether the registry superseded `model` — i.e. the spawn picker hides it by
 * default in favor of its replacement. Display-only: a superseded model stays
 * spawnable and config-valid; settings/config can still switch back to it.
 */
export function isSuperseded(modelsData: ModelsResponse | undefined, model: string): boolean {
  return Boolean(modelsData?.models[model]?.superseded_by);
}
