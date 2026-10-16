"use client";

import { ArrowLeft, House, RefreshCw } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { ChatMarkdown } from "@/components/content/markdown";
import type { ToUserDocument } from "@/lib/contracts/types";
import { useToUser } from "@/lib/context/use-to-user";
import { cn } from "@/lib/format/utils";
import { formatRelative } from "@/lib/format/time";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout/layout";
import { API_BASE } from "@/lib/transport/api-base";

export function ToUserDocumentView({ agentId, document, failed, pending, refresh }: {
  agentId: number;
  document: ToUserDocument | undefined;
  failed: boolean;
  pending: boolean;
  refresh: () => void;
}) {
  const t = useTranslations("toUser");
  const [references, setReferences] = useState<string[]>([]);
  const linked = useToUser(references.length ? agentId : null, references);
  const current = references.length ? linked.data : document;
  const loading = references.length ? linked.isFetching : pending;
  const error = references.length ? linked.isError : failed;
  const content = current?.content;
  const resources = new Map(Object.entries(current?.resources ?? {}).map(([href, resource]) => {
    const query = new URLSearchParams();
    for (const reference of [...references, resource.id]) query.append("reference", reference);
    if (resource.version) query.set("v", resource.version);
    const fragment = href.includes("#") ? href.slice(href.indexOf("#")) : "";
    return [href, { ...resource, url: `${API_BASE}/api/agents/${agentId}/to-user/resource?${query}${fragment}` }];
  }));
  const byUrl = new Map([...resources.values()].map((resource) => [resource.url, resource]));
  return (
    <section className={cn(FLEX, MIN_H_0, FLEX_1, FLEX_COL)} aria-label={t("title")}>
      <div className={cn("h-9 shrink-0 items-center gap-2 border-b px-3 text-[11px] text-muted-foreground", FLEX)}>
        <button title={t("home")} aria-label={t("home")} className="rounded p-1 hover:bg-accent" onClick={() => setReferences([])}>
          <House className="size-3.5" />
        </button>
        {references.length > 0 && <button title={t("back")} aria-label={t("back")} className="rounded p-1 hover:bg-accent" onClick={() => setReferences((value) => value.slice(0, -1))}>
          <ArrowLeft className="size-3.5" />
        </button>}
        <span className={cn("truncate", MIN_W_0, FLEX_1)}>
          {current?.modified_at ? t("updated", { time: formatRelative(current.modified_at) }) : t("sharedContext")}
        </span>
        <button title={t("refresh")} aria-label={t("refresh")} onClick={() => { if (references.length) void linked.refetch(); else refresh(); }} disabled={loading} className="rounded p-1 hover:bg-accent disabled:opacity-50">
          <RefreshCw className="size-3.5" />
        </button>
      </div>
      {error && content && <div role="status" className="border-b bg-amber-500/10 px-4 py-2 text-xs text-amber-700 dark:text-amber-300">{t("stale")}</div>}
      {!content ? (
        <div className={cn("items-center justify-center px-8 text-center text-sm text-muted-foreground", FLEX, FLEX_1)}>
          {loading ? t("loading") : error ? t("unavailable") : current?.legacy_html ? t("legacyHtml") : t("empty")}
        </div>
      ) : (
        <div key={references.join("/")} className={cn("overflow-auto p-5 text-sm", MIN_H_0, FLEX_1)}>
          <ChatMarkdown content={content} resolveUrl={(href) => {
            const resource = resources.get(href);
            return resource?.url ?? (/^(https?:|mailto:|#)/i.test(href) ? href : "about:blank");
          }} components={{
            a: ({ href, children }) => {
              const resource = href ? byUrl.get(href) : undefined;
              const external = !href?.startsWith("#");
              return <a href={href} target={external ? "_blank" : undefined} rel={external ? "noopener noreferrer" : undefined} onClick={(event) => {
                if (!resource?.markdown || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
                event.preventDefault();
                setReferences((value) => [...value, resource.id]);
              }}>{children}</a>;
            },
          }} />
        </div>
      )}
    </section>
  );
}
