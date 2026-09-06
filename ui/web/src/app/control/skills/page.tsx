"use client";

// /control#skills — this gateway host's installed skills.
//
// Read: GET /api/skills returns a SkillsView — every top-level directory in the
// host's `$AVA_HOME/skills/` load dir correlated with the install registry:
// name, source layer (core=repo / plugin / machine=user / untracked), and the
// `enabled` toggle the skill scanner honors. Per-machine (skills aren't a
// cross-machine matrix like plugins/MCP).
//
// Adding one is not a form: PackageDraftEntry hands a natural-language request
// to an installer agent (POST /api/packages/draft) that finds, installs, and
// verifies the skill in its own conversation.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";

import { PackageDraftEntry } from "@/components/package-draft-entry";
import { Switch } from "@/components/ui/switch";

import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { api } from "@/lib/api";
import { errMsg } from "@/lib/errors";
import { useStore } from "@/lib/store";
import type { SkillLayer, SkillsView } from "@/lib/types";

import { useSectionVisible } from "../_visibility";
import { FLEX } from "@/lib/layout";
import { cn } from "@/lib/utils";

const SKILLS_QUERY_KEY = ["skills"] as const;

function layerLabel(layer: SkillLayer, t: ReturnType<typeof useTranslations<"skills">>): string {
  // The key mapping is literal — TS narrows each branch so the key is a
  // checked member of the skills namespace.
  switch (layer) {
    case "core":
      return t("layerCore");
    case "plugin":
      return t("layerPlugin");
    case "machine":
      return t("layerMachine");
    case "untracked":
      return t("layerUntracked");
  }
}

export default function SkillsPage() {
  const t = useTranslations("skills");
  const showToast = useStore((s) => s.showToast);
  const visible = useSectionVisible();
  const queryClient = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: SKILLS_QUERY_KEY,
    queryFn: api.getSkills,
    enabled: visible,
  });

  const toggleMutation = useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) =>
      api.putSkillEnabled(name, enabled),
    // Without an onError the switch silently snapped back (invalidate pulled
    // the server truth) and the user never knew the toggle failed.
    onError: (err: unknown) => showToast(`Failed to toggle skill: ${errMsg(err)}`),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: SKILLS_QUERY_KEY });
    },
  });

  // The install entry stays mounted through every state — you can ask for a new
  // skill while the list is still loading, or when the read failed.
  return (
    <div className="space-y-3">
      <p className="text-xs text-muted-foreground">
        {t("intro")}
      </p>
      <PackageDraftEntry kind="skill" />
      <SkillsTable
        data={data}
        isLoading={isLoading}
        error={error}
        onToggle={(name, enabled) => toggleMutation.mutate({ name, enabled })}
        busy={toggleMutation.isPending}
      />
    </div>
  );
}

function SkillsTable({
  data,
  isLoading,
  error,
  onToggle,
  busy,
}: {
  data: SkillsView | undefined;
  isLoading: boolean;
  error: unknown;
  onToggle: (name: string, enabled: boolean) => void;
  busy: boolean;
}) {
  const t = useTranslations("skills");
  if (isLoading) {
    return (
      <div className={cn("justify-center py-12", FLEX)}>
        <Loader2 className="size-6 animate-spin text-muted-foreground" />
      </div>
    );
  }
  if (error) {
    return (
      <p className="text-sm text-muted-foreground">
        {t("couldntLoad")}
      </p>
    );
  }
  if (!data) return <p className="text-sm text-muted-foreground">{t("noData")}</p>;
  if (data.skills.length === 0) {
    return <p className="text-xs text-muted-foreground">{t("noSkills")}</p>;
  }

  return (
    <div className="border border-border rounded-md overflow-x-auto">
      <Table className="[&_th]:border-r [&_th]:border-border [&_th:last-child]:border-r-0 [&_td]:border-r [&_td]:border-border [&_td:last-child]:border-r-0">
        <TableHeader>
          <TableRow>
            <TableHead className="min-w-48">{t("skill")}</TableHead>
            <TableHead>{t("source")}</TableHead>
            <TableHead className="text-center">{t("enabled")}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {data.skills.map((s) => (
            <TableRow key={s.name}>
              <TableCell className="align-top">
                <span className="font-mono font-medium">{s.name}</span>
              </TableCell>
              <TableCell className="align-top">
                <span className="text-xs text-muted-foreground border border-border rounded px-1">
                  {layerLabel(s.layer, t)}
                </span>
              </TableCell>
              <TableCell className="text-center align-top">
                <Switch
                  checked={s.enabled}
                  onCheckedChange={(checked) => onToggle(s.name, checked)}
                  disabled={s.layer === "untracked" || busy}
                  aria-label={t("toggle", { name: s.name })}
                />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
