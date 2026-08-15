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
  grok: "Grok",
};

export function providerLabel(provider: string): string {
  return (
    PROVIDER_LABELS[provider] ??
    (provider.length > 0 ? provider[0].toUpperCase() + provider.slice(1) : provider)
  );
}

/**
 * Models grouped by provider, in API order (both provider order and
 * within-provider model order come straight from `modelsData.providers` —
 * see ModelsResponse). An optional predicate filters individual models
 * (e.g. the spawn picker excludes user-hidden models); a provider left
 * with no models after filtering is dropped rather than rendered empty.
 */
export function groupedModels(
  modelsData: ModelsResponse | undefined,
  filter?: (model: string) => boolean,
): [provider: string, models: string[]][] {
  if (!modelsData) return [];
  return Object.entries(modelsData.providers)
    .map(([provider, models]): [string, string[]] => [
      provider,
      filter ? models.filter(filter) : models,
    ])
    .filter(([, models]) => models.length > 0);
}
