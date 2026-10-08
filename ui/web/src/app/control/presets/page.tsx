"use client";

// /control#presets — preset management and the Preset Maker conversation.
// Creation uses a plain agent spawn, like the schedule writer handoff. The
// skill owns discovery and composition; an empty request opens the maker so
// the user can describe the role in its conversation.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Pencil, Sparkles, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/transport/api";
import { errMsg } from "@/lib/contracts/errors";
import { useStore } from "@/lib/state/store";
import { formatRelative } from "@/lib/format/time";
import type { PresetUpdate, PresetView } from "@/lib/contracts/types";

import { PRESETS_QUERY_KEY, presetAnchorId } from "../_sections";
import { useSectionVisible } from "../_visibility";
import { FLEX, FLEX_1, FLEX_COL, MIN_W_0 } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

const PRESET_MAKER_PROMPT =
  "Read and follow ava.skills.ava_guide.presets as the preset maker to create or improve a reusable agent preset. Request:\n\n";

export default function PresetsPage() {
  const t = useTranslations("presets");
  const queryClient = useQueryClient();
  const showToast = useStore((s) => s.showToast);
  const setActiveId = useStore((s) => s.setActiveId);
  const router = useRouter();
  const visible = useSectionVisible();

  const { data: presets, isLoading, error } = useQuery({
    queryKey: PRESETS_QUERY_KEY,
    queryFn: api.listPresets,
    enabled: visible,
  });

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: PRESETS_QUERY_KEY });
  };

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deletePreset(id),
    onSuccess: invalidate,
    onError: (e: unknown) => showToast(t("deleteFailed", { error: errMsg(e) })),
  });

  const [nl, setNl] = useState("");
  const draftMutation = useMutation({
    mutationFn: (text: string) =>
      api.spawnAgent({
        prompt: PRESET_MAKER_PROMPT + (text || "Help me design a reusable agent preset."),
        prompt_source: "user",
        label: "ava-preset-maker",
        config: { skills_to_expand_at_start: ["ava-guide:presets"] },
      }),
    onSuccess: (res) => {
      setNl("");
      setActiveId(res.id);
      showToast(t("writerCreated", { id: res.id }));
      router.push("/");
    },
    onError: (e: unknown) => showToast(t("draftFailed", { error: errMsg(e) })),
  });

  if (isLoading) {
    return (
      <div className={cn("justify-center py-12", FLEX)}>
        <Loader2 className="size-6 animate-spin text-muted-foreground" />
      </div>
    );
  }
  if (error) {
    return (
      <div className="p-8 text-center text-sm text-muted-foreground">
        {t("couldntLoad")}
      </div>
    );
  }

  const list = presets ?? [];

  return (
    <div className="space-y-4">
      <p className="text-xs text-muted-foreground">
        {t("intro")}
      </p>

      {/* --- Natural-language create --- */}
      <div className={cn("items-center gap-2", FLEX)}>
        <Input
          className="h-8 text-xs"
          placeholder={t("describePreset")}
          value={nl}
          onChange={(e) => setNl(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !draftMutation.isPending) draftMutation.mutate(nl.trim());
          }}
        />
        <Button
          type="button"
          size="sm"
          disabled={draftMutation.isPending}
          onClick={() => draftMutation.mutate(nl.trim())}
        >
          {draftMutation.isPending ? (
            <Loader2 className="size-3.5 animate-spin mr-1" />
          ) : (
            <Sparkles className="size-3.5 mr-1" />
          )}
          {t("describe")}
        </Button>
      </div>

      <div className="space-y-3">
        {list.length === 0 && (
          <p className="text-xs text-muted-foreground py-4 text-center">{t("noPresets")}</p>
        )}
        {list.map((p) => (
          <PresetCard
            key={p.id}
            preset={p}
            onDelete={() => {
              if (window.confirm(t("deleteConfirm", { label: p.label }))) deleteMutation.mutate(p.id);
            }}
            onEdited={invalidate}
          />
        ))}
      </div>
    </div>
  );
}

function PresetCard({
  preset,
  onDelete,
  onEdited,
}: {
  preset: PresetView;
  onDelete: () => void;
  onEdited: () => void;
}) {
  const t = useTranslations("presets");
  const showToast = useStore((s) => s.showToast);
  const [editing, setEditing] = useState(false);
  const [label, setLabel] = useState(preset.label);
  const [description, setDescription] = useState(preset.description ?? "");

  const updateMutation = useMutation({
    mutationFn: (body: PresetUpdate) => api.updatePreset(preset.id, body),
    onSuccess: () => {
      showToast(t("updated"));
      setEditing(false);
      onEdited();
    },
    onError: (e: unknown) => showToast(t("updateFailed", { error: errMsg(e) })),
  });

  const startEdit = () => {
    setLabel(preset.label);
    setDescription(preset.description ?? "");
    setEditing(true);
  };

  const onSave = () => {
    updateMutation.mutate({
      label: label.trim(),
      description: description.trim() || null,
    });
  };

  return (
    <div
      id={presetAnchorId(preset.name)}
      className="scroll-mt-4 rounded-md border border-border"
      data-testid={`preset-card-${preset.name}`}
    >
      <div className={cn(FLEX)}>
        <div className={cn("w-72 shrink-0 px-3 py-2.5", FLEX, FLEX_COL)}>
          <div className={cn("items-start justify-between gap-2", FLEX)}>
            <div className={cn(MIN_W_0)}>
              <div className="text-sm font-medium">{preset.label}</div>
              <div className="text-[11px] text-muted-foreground font-mono">{preset.name}</div>
            </div>
            <div className={cn("gap-0.5", FLEX)}>
              <IconButton
                label={t("edit")}
                onClick={() => (editing ? setEditing(false) : startEdit())}
                icon={<Pencil className="size-3.5" />}
              />
              <IconButton label={t("delete")} onClick={onDelete} icon={<Trash2 className="size-3.5" />} />
            </div>
          </div>
          {preset.description && (
            <p className="mt-1.5 text-xs text-muted-foreground">{preset.description}</p>
          )}
          <div className="mt-auto pt-2 text-[11px] text-muted-foreground">
            Updated {formatRelative(preset.updated_at)}
          </div>
        </div>
        <pre className={cn("whitespace-pre-wrap break-words rounded-r-md border-l border-border bg-muted/50 px-3 py-2.5 font-mono text-[11px] leading-relaxed", MIN_W_0, FLEX_1)}>
          {JSON.stringify(preset.config, null, 2)}
        </pre>
      </div>
      {editing && (
        <div className="border-t border-border bg-muted/30 p-3 space-y-2">
          <label className="block text-xs">
            Label
            <Input
              className="mt-1 h-8 text-xs"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
            />
          </label>
          <label className="block text-xs">
            Description
            <Input
              className="mt-1 h-8 text-xs"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          <p className="text-[11px] text-muted-foreground">
            The config itself isn&apos;t hand-edited here — delete this preset and describe a new
            one above to reshape it.
          </p>
          <div className={cn("items-center gap-2", FLEX)}>
            <Button
              type="button"
              size="sm"
              disabled={!label.trim() || updateMutation.isPending}
              onClick={onSave}
            >
              {updateMutation.isPending ? <Loader2 className="size-3.5 animate-spin mr-1" /> : null}
              Save
            </Button>
            <Button type="button" size="sm" variant="ghost" onClick={() => setEditing(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

function IconButton({
  label,
  onClick,
  icon,
}: {
  label: string;
  onClick: () => void;
  icon: React.ReactNode;
}) {
  return (
    <Button
      type="button"
      size="icon"
      variant="ghost"
      className="size-7 text-muted-foreground"
      onClick={onClick}
      aria-label={label}
    >
      {icon}
    </Button>
  );
}
