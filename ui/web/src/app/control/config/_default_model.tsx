"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { Loader2 } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/transport/api";
import { errMsg } from "@/lib/contracts/errors";
import { groupedModels, isSuperseded, providerLabel } from "@/lib/contracts/models";
import { useStore } from "@/lib/state/store";

import { useSectionVisible } from "../_visibility";
import { FLEX } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

const DEFAULT_MODEL_QUERY_KEY = ["default-model"] as const;
const MODELS_QUERY_KEY = ["models"] as const;

// meiva:link ui/web/src/app/control/config/docs/configuration.meiva.okf.md
export function DefaultModelPanel({ id }: { id: string }) {
  const t = useTranslations("config");
  const visible = useSectionVisible();
  const qc = useQueryClient();
  const showToast = useStore((s) => s.showToast);

  const [picked, setPicked] = useState<string | null>(null);

  const current = useQuery({
    queryKey: DEFAULT_MODEL_QUERY_KEY,
    queryFn: api.getDefaultModel,
    enabled: visible,
  });
  const models = useQuery({
    queryKey: MODELS_QUERY_KEY,
    queryFn: () => api.getModels(),
    staleTime: Infinity,
    enabled: visible,
  });

  const save = useMutation({
    mutationFn: (model: string) => api.putDefaultModel(model),
    onSuccess: () => {
      setPicked(null);
      showToast(t("defaultModelSaved"));
    },
    onError: (err: unknown) => showToast(t("saveFailed", { error: errMsg(err) })),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: DEFAULT_MODEL_QUERY_KEY });
      void qc.invalidateQueries({ queryKey: MODELS_QUERY_KEY });
    },
  });

  const stored = current.data?.model ?? "";
  const value = picked ?? stored;
  const dirty = picked !== null && picked !== stored;

  const groups = groupedModels(models.data, (m) => !isSuperseded(models.data, m));

  return (
    <div id={id} className="scroll-mt-4 space-y-2" data-testid="config-default-model">
      <h3 className="text-sm font-semibold text-muted-foreground">{t("defaultModel")}</h3>
      <p className="text-xs leading-snug text-muted-foreground">
        The model a new agent is born on when the spawn names none. Agents that already
        exist keep the model frozen at their own birth — changing this moves nobody.
      </p>

      {current.error || models.error ? (
        <p className="text-sm text-muted-foreground">
          Couldn&apos;t load the default model — check the gateway and reload.
        </p>
      ) : (
        <div className={cn("items-center gap-2", FLEX)}>
          <select
            id="default-model-select"
            aria-label="Default model"
            data-testid="select-default-model"
            value={value}
            disabled={current.isLoading || models.isLoading || save.isPending}
            onChange={(e) => setPicked(e.target.value)}
            className="cursor-pointer rounded border border-border bg-transparent px-1.5 py-1 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          >
            {value === "" && (
              <option value="" disabled>
                {t("loadingModels")}
              </option>
            )}

            {stored !== "" && !groups.some(([, ids]) => ids.includes(stored)) && (
              <option value={stored}>{stored}</option>
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
          <Button
            type="button"
            size="sm"
            disabled={!dirty || save.isPending}
            data-testid="save-default-model"
            onClick={() => picked !== null && save.mutate(picked)}
          >
            {save.isPending && <Loader2 className="mr-1 size-3.5 animate-spin" />}
            Save
          </Button>
          {current.data?.source === "config" && (
            <span
              className="text-[11px] italic text-muted-foreground"
              data-testid="default-model-source"
            >
              no cluster choice yet — showing the .env / code default
            </span>
          )}
        </div>
      )}
    </div>
  );
}
