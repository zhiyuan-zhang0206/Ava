"use client";

// /control#guide — the Ava Guide entry. Describe an operations task in natural
// language and it spawns an ava-guide agent (POST /api/guide/draft), then jumps
// to that conversation to finish it. Mirrors the Schedules page's writer entry:
// the fixed prompt lives server-side and points at ava.skills.ava_guide (the
// map for operating the cluster via the `ava` CLI — start/update, tracks, MCP
// servers, installing skills/plugins, presets, schedules). This is where the
// non-actionable config knobs the panel omits get handled: ask the agent.

import { useMutation } from "@tanstack/react-query";
import { Loader2, Sparkles } from "lucide-react";
import { useTranslations } from "next-intl";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { errMsg } from "@/lib/errors";
import { useStore } from "@/lib/store";
import { FLEX } from "@/lib/layout";
import { cn } from "@/lib/utils";

export default function GuidePage() {
  const t = useTranslations("guide");
  const showToast = useStore((s) => s.showToast);
  const setActiveId = useStore((s) => s.setActiveId);
  const router = useRouter();
  const [nl, setNl] = useState("");

  const draftMutation = useMutation({
    mutationFn: (text: string) => api.draftGuide(text),
    onSuccess: (res) => {
      setNl("");
      setActiveId(res.agent_id);
      showToast(t("started", { id: res.agent_id }));
      router.push("/");
    },
    onError: (err: unknown) => showToast(t("failed", { error: errMsg(err) })),
  });

  const submit = () => {
    if (nl.trim()) draftMutation.mutate(nl.trim());
  };

  return (
    <div className="space-y-2">
      <p className="text-xs text-muted-foreground">
        {t("intro")}
      </p>
      <div className={cn("items-center gap-2", FLEX)}>
        <Input
          className="h-8 text-xs"
          placeholder={t("describeTask")}
          value={nl}
          onChange={(e) => setNl(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
          }}
        />
        <Button type="button" size="sm" disabled={!nl.trim() || draftMutation.isPending} onClick={submit}>
          {draftMutation.isPending ? (
            <Loader2 className="size-3.5 animate-spin mr-1" />
          ) : (
            <Sparkles className="size-3.5 mr-1" />
          )}
          {t("ask")}
        </Button>
      </div>
    </div>
  );
}
