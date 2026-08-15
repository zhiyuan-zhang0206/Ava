"use client";

// The "add a package" entry shared by the Skills / Plugins / MCP sections.
//
// Describe a capability in natural language -> POST /api/packages/draft spawns
// an ava-package-installer agent -> jump to that conversation, where the agent
// finds candidates, confirms before installing anything that runs code,
// installs, verifies with a test agent, and reports whether it is any good.
//
// Deliberately NOT a URL/spec form: the user generally cannot tell a good
// package from a bad one, which is the whole reason an agent owns the flow.
// Mirrors the Guide/Schedules writer entries — the fixed prompt lives
// server-side; this is just the input and the jump.

import { useMutation } from "@tanstack/react-query";
import { Loader2, Sparkles } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api } from "@/lib/api";
import { errMsg } from "@/lib/errors";
import { useStore } from "@/lib/store";
import type { PackageKind } from "@/lib/types";
import { FLEX } from "@/lib/layout";
import { cn } from "@/lib/utils";

// All three entries can be mounted at once (the Control page stacks every
// section), so placeholder + accessible name are distinct per kind.
const COPY: Record<PackageKind, { label: string; placeholder: string }> = {
  skill: {
    label: "Find and install a skill",
    placeholder: "Describe a skill you want — e.g. “write release notes the way we do”",
  },
  plugin: {
    label: "Find and install a plugin",
    placeholder: "Describe a plugin you want — e.g. “review my PRs like a senior engineer”",
  },
  mcp: {
    label: "Find and install an MCP server",
    placeholder: "Describe a tool you want to reach — e.g. “query our Linear issues”",
  },
};

export function PackageDraftEntry({ kind }: { kind: PackageKind }) {
  const showToast = useStore((s) => s.showToast);
  const setActiveId = useStore((s) => s.setActiveId);
  const router = useRouter();
  const [nl, setNl] = useState("");
  const { label, placeholder } = COPY[kind];

  const draftMutation = useMutation({
    mutationFn: (text: string) => api.draftPackage(kind, text),
    onSuccess: (res) => {
      setNl("");
      setActiveId(res.agent_id);
      showToast(`Installer #${res.agent_id} started — continue in the conversation`);
      router.push("/");
    },
    onError: (err: unknown) => showToast(`Install failed: ${errMsg(err)}`),
  });

  const submit = () => {
    if (nl.trim()) draftMutation.mutate(nl.trim());
  };

  return (
    <div className={cn("items-center gap-2", FLEX)}>
      <Input
        className="h-8 text-xs"
        placeholder={placeholder}
        value={nl}
        onChange={(e) => setNl(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
        }}
      />
      <Button
        type="button"
        size="sm"
        disabled={!nl.trim() || draftMutation.isPending}
        aria-label={label}
        onClick={submit}
      >
        {draftMutation.isPending ? (
          <Loader2 className="size-3.5 animate-spin mr-1" />
        ) : (
          <Sparkles className="size-3.5 mr-1" />
        )}
        Find &amp; install
      </Button>
    </div>
  );
}
