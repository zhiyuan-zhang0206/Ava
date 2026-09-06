"use client";

// The cluster's default model — the model a NEW agent is born on.
//
// Read: GET /api/config/default-model -> DefaultModelView { model, source }.
// `source` is "cluster" when a choice has been made here and "config" when the
// .env / code default is showing through.
// Write: PUT /api/config/default-model -> DefaultModelView. Its own narrow
// endpoint, deliberately not a field on the full-replace PUT /api/config.
// Options: GET /api/models (the same spawnable roster the spawn picker renders).
//
// Editing this never moves an agent that already exists: each agent's model is
// frozen onto its own row at birth. That is the whole reason this is safe to
// expose as a one-click dropdown on a live cluster.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { Loader2 } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { errMsg } from "@/lib/errors";
import { groupedModels, isSuperseded, providerLabel } from "@/lib/models";
import { useStore } from "@/lib/store";

import { useSectionVisible } from "../_visibility";
import { FLEX } from "@/lib/layout";
import { cn } from "@/lib/utils";

const DEFAULT_MODEL_QUERY_KEY = ["default-model"] as const;
const MODELS_QUERY_KEY = ["models"] as const;

export function DefaultModelPanel({ id }: { id: string }) {
  const t = useTranslations("config");
  const visible = useSectionVisible();
  const qc = useQueryClient();
  const showToast = useStore((s) => s.showToast);
  // Null until the user touches the dropdown, so a refetch that changes the
  // stored value is not silently overwritten by a stale local pick.
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
    },
  });

  const stored = current.data?.model ?? "";
  const value = picked ?? stored;
  const dirty = picked !== null && picked !== stored;
  // Superseded models stay valid as a stored default (agents born before the
  // swap keep running on them), but the dropdown offers only the current
  // picker roster — same visibility rule as the spawn picker.
  const groups = groupedModels(models.data, (m) => !isSuperseded(models.data, m));

  return (
    <div id={id} className="scroll-mt-4 space-y-2" data-testid="config-default-model">
      <h3 className="text-sm font-semibold text-muted-foreground">{t("defaultModel")}</h3>
      <p className="text-xs leading-snug text-muted-foreground">
        The model a new agent is born on when the spawn names none. Agents that already
        exist keep the model frozen at their own birth — changing this moves nobody.
      </p>

      {current.error ? (
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
            {/* Keep the stored value selectable even if it left the roster, so the
                dropdown reports the truth instead of silently showing another model. */}
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
              className="text-xs italic text-muted-foreground"
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
