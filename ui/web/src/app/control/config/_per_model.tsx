"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { api } from "@/lib/transport/api";
import { groupedModels, providerLabel } from "@/lib/contracts/models";
import type { ResolvedFieldView } from "@/lib/contracts/types";

import { fieldLabel, formatConfigValue } from "../_config_groups";
import { useSectionVisible } from "../_visibility";
import { FLEX, FLEX_1, MIN_W_0 } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

const MODELS_QUERY_KEY = ["models"] as const;
const resolvedQueryKey = (model: string) => ["config-resolved", model] as const;

const SOURCE_LABEL: Record<ResolvedFieldView["source"], string> = {
  explicit: ".env",
  "model-default": "model default",
  "shared-default": "shared default",
};

function shadowedLayers(
  field: ResolvedFieldView,
  t: ReturnType<typeof useTranslations<"config">>,
): string[] {
  const out: string[] = [];
  if (field.source === "explicit" && field.model_default != null) {
    out.push(t("modelDefault", { value: formatConfigValue(field.model_default) }));
  }
  if (field.source !== "shared-default") {
    out.push(t("sharedDefault", { value: formatConfigValue(field.shared_default) }));
  }
  return out;
}

// meiva:link ui/web/src/app/control/config/docs/configuration.meiva.okf.md
export function PerModelPanel({
  id,
  onFocusField,
}: {
  id: string;

  onFocusField: (name: string) => void;
}) {
  const t = useTranslations("config");
  const visible = useSectionVisible();
  const [picked, setPicked] = useState<string | null>(null);

  const models = useQuery({
    queryKey: MODELS_QUERY_KEY,
    queryFn: () => api.getModels(),
    staleTime: Infinity,
    enabled: visible,
  });

  const model = picked ?? models.data?.default ?? "";

  const resolved = useQuery({
    queryKey: resolvedQueryKey(model),
    queryFn: () => api.getResolvedConfig(model),
    enabled: visible && !models.error && model !== "",
  });

  const groups = groupedModels(models.data);

  return (
    <div id={id} className="scroll-mt-4 space-y-2" data-testid="config-per-model">
      <h3 className="text-sm font-semibold text-muted-foreground">{t("perModelValues")}</h3>
      <p className="text-xs text-muted-foreground leading-snug">
        Settings that resolve per model: a shared default, overridden by the model&apos;s own
        default, overridden by an explicit .env value. Both defaults are code
        (base/lm/registry.py) — to change one for every model, edit the field below. A
        per-agent overlay set at spawn beats all three and is not visible here.
      </p>

      <div className={cn("items-center gap-2", FLEX)}>
        <label htmlFor="per-model-model" className="text-xs text-muted-foreground">
          {t("model")}
        </label>
        <select
          id="per-model-model"
          aria-label={t("model")}
          value={model}
          onChange={(e) => setPicked(e.target.value)}
          disabled={Boolean(models.error) || model === ""}
          className="text-xs bg-transparent border border-border rounded px-1.5 py-1 text-foreground focus:outline-none focus:ring-1 focus:ring-ring cursor-pointer"
        >
          {model === "" && (
            <option value="" disabled>
              {models.error ? "Unavailable" : t("loadingModels")}
            </option>
          )}
          {groups.map(([provider, ids]) => (
            <optgroup key={provider} label={providerLabel(provider)}>
              {ids.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
        {resolved.data?.registered === false && (
          <span className="text-[11px] italic text-muted-foreground" data-testid="per-model-unregistered">
            {t("notInRegistry")}
          </span>
        )}
      </div>

      {models.error && (
        <p role="alert" className="text-xs text-muted-foreground">
          Model settings are unavailable. Check the gateway configuration and reload.
        </p>
      )}
      {resolved.isLoading && <p className="text-xs text-muted-foreground">{t("loading")}</p>}
      {resolved.error && (
        <p className="text-xs text-muted-foreground">
          {t("couldntResolve")}
        </p>
      )}

      {!models.error && resolved.data && (
        <div className="border border-border rounded-md divide-y divide-border">
          {resolved.data.fields.map((field) => (
            <PerModelRow key={field.name} field={field} onFocusField={onFocusField} />
          ))}
        </div>
      )}
    </div>
  );
}

function PerModelRow({
  field,
  onFocusField,
}: {
  field: ResolvedFieldView;
  onFocusField: (name: string) => void;
}) {
  const t = useTranslations("config");
  const shadowed = shadowedLayers(field, t);
  return (
    <div className={cn("items-start justify-between gap-4 px-3 py-1.5", FLEX)}>
      <div className={cn("space-y-0.5", FLEX_1, MIN_W_0)}>
        <div className={cn("items-center gap-2 flex-wrap", FLEX)}>

          <button
            type="button"
            onClick={() => onFocusField(field.name)}
            title={t("gotoEditor", { env: field.env_var })}
            data-testid={`per-model-goto-${field.name}`}
            className="font-medium break-all underline decoration-dotted underline-offset-2 hover:text-foreground text-left"
          >
            {fieldLabel(field.env_var)}
          </button>
          <span
            className="rounded border border-dashed border-border px-1 text-[11px] text-muted-foreground/80"
            data-testid={`per-model-source-${field.name}`}
          >
            {SOURCE_LABEL[field.source]}
          </span>
          {field.per_agent && (
            <span className="rounded border border-border px-1 text-[11px] text-muted-foreground">
              per-agent
            </span>
          )}
        </div>
        {shadowed.length > 0 && (
          <p className="text-[11px] text-muted-foreground" data-testid={`per-model-shadowed-${field.name}`}>
            shadows {shadowed.join(", ")}
          </p>
        )}
      </div>

      <span
        className="shrink-0 text-xs max-w-48 truncate"
        title={formatConfigValue(field.effective_value)}
        data-testid={`per-model-value-${field.name}`}
      >
        {formatConfigValue(field.effective_value)}
      </span>
    </div>
  );
}
