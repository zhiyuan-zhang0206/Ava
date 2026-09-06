"use client";

// /control#presets — config-preset management: list, create (natural-language
// via a preset-writer agent), tweak label/description, delete.
//
// A preset is a named per-agent config-overlay template (llm_model, plugin
// per_agent fields, ...). Selecting one in the spawn picker seeds the new
// agent's config from it; an explicit spawn config still wins per field.
// Backed by /api/presets (gateway/routers/presets.py). `config` is stored as
// an opaque JSON object — it is validated when a spawned agent applies it, not
// on this page, so a bad overlay surfaces as a failed spawn, not a failed save.
//
// Hand-writing that config JSON is not a UI task — the field names and skill
// combinations that make a good preset live in ava-guide.presets, not in a
// frontend form. So creation mirrors the Schedules page's natural-language
// create, composed straight from existing primitives: a short prompt pointing
// a new agent at ava.skills.ava_guide.presets plus the user's ask, spawned via
// the plain api.spawnAgent() (no dedicated backend endpoint), then handing
// over its conversation; this page never opens a raw config editor. Reshaping
// a preset's config is the same agent-driven path — delete and re-describe
// rather than hand-edit; label/description are plain text, so those stay
// editable in place.
//
// Each preset renders as a full-width horizontal card (metadata + actions on
// the left, the pretty-printed JSON overlay on the right) with a stable
// element id (presetAnchorId) — the nav's dynamic Presets sub-links jump to
// these anchors.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Pencil, Sparkles, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { errMsg } from "@/lib/errors";
import { useStore } from "@/lib/store";
import { formatRelative } from "@/lib/time";
import type { PresetUpdate, PresetView } from "@/lib/types";

import { PRESETS_QUERY_KEY, presetAnchorId } from "../_sections";
import { useSectionVisible } from "../_visibility";
import { FLEX, FLEX_1, FLEX_COL, MIN_W_0 } from "@/lib/layout";
import { cn } from "@/lib/utils";

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

  // --- natural-language create: spawn a writer agent, open its conversation ---
  //
  // No dedicated backend endpoint — a plain spawn whose first message points
  // the new agent at the skill that knows how to write a preset. The prompt
  // stays minimal: HOW to write a preset lives in ava-guide.presets, not here.
  const [nl, setNl] = useState("");
  const draftMutation = useMutation({
    mutationFn: (text: string) =>
      api.spawnAgent({
        prompt: t("followSkill", { text }),
        prompt_source: "user",
        label: "preset_writer",
      }),
    onSuccess: (res) => {
      setNl("");
      setActiveId(res.id);
      showToast(t("writerStarted", { id: res.id }));
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
            if (e.key === "Enter" && nl.trim()) draftMutation.mutate(nl.trim());
          }}
        />
        <Button
          type="button"
          size="sm"
          disabled={!nl.trim() || draftMutation.isPending}
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
          <div className="mt-auto pt-2 text-xs text-muted-foreground">
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
          <p className="text-xs text-muted-foreground">
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
