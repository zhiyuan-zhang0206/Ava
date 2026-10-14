"use client";

import { ExternalLink, House, RefreshCw } from "lucide-react";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout/layout";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type { ToUserDocument } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { API_BASE } from "@/lib/transport/api-base";
import { formatRelative } from "@/lib/format/time";

export function ToUserDocumentView({ agentId, document, failed, pending, refresh }: {
  agentId: number;
  document: ToUserDocument | undefined;
  failed: boolean;
  pending: boolean;
  refresh: () => void;
}) {
  const t = useTranslations("toUser");
  const frame = useRef<HTMLIFrameElement>(null);
  const [navigation, setNavigation] = useState<{ url: string | null; generation: number }>({ url: null, generation: 0 });
  const documentUrl = `${API_BASE}/api/agents/${agentId}/to-user/document`;
  const content = document?.content;
  useEffect(() => {
    const navigate = (event: MessageEvent) => {
      if (event.source !== frame.current?.contentWindow) return;
      const data: unknown = event.data;
      if (!data || typeof data !== "object" || !("type" in data) || data.type !== "ava:to-user:navigate" || !("href" in data) || typeof data.href !== "string") return;
      let url: URL;
      try { url = new URL(data.href); } catch { return; }
      if (url.protocol !== "http:" && url.protocol !== "https:") return;
      setNavigation((n) => ({ url: url.href, generation: n.generation + 1 }));
    };
    window.addEventListener("message", navigate);
    return () => window.removeEventListener("message", navigate);
  }, []);
  return (
    <section className={cn("", FLEX, MIN_H_0, FLEX_1, FLEX_COL)} aria-label={t("title")}>
      <div className={cn("h-9 shrink-0 items-center gap-2 border-b px-3 text-[11px] text-muted-foreground", FLEX)}>
        <button title={t("home")} aria-label={t("home")} className="rounded p-1 hover:bg-accent" onClick={() => setNavigation((n) => ({ url: null, generation: n.generation + 1 }))}>
          <House className="size-3.5" />
        </button>
        <span className={cn("truncate", MIN_W_0, FLEX_1)}>
          {document?.modified_at ? t("updated", { time: formatRelative(document.modified_at) }) : t("sharedContext")}
        </span>
        <button title={t("refresh")} aria-label={t("refresh")} onClick={refresh} disabled={pending} className="rounded p-1 hover:bg-accent disabled:opacity-50">
          <RefreshCw className="size-3.5" />
        </button>
        {(navigation.url != null || (Boolean(content) && document?.format === "html")) && (
          <a href={navigation.url ?? documentUrl} target="_blank" rel="noopener noreferrer" title={t("openDocument")} aria-label={t("openDocument")} className="rounded p-1 hover:bg-accent">
            <ExternalLink className="size-3.5" />
          </a>
        )}
      </div>
      {failed && content && <div role="status" className="border-b bg-amber-500/10 px-4 py-2 text-xs text-amber-700 dark:text-amber-300">{content ? t("stale") : t("unavailable")}</div>}
      {!content && !navigation.url ? (
        <div className={cn("items-center justify-center px-8 text-center text-sm text-muted-foreground", FLEX, FLEX_1)}>
          {pending ? t("loading") : failed ? t("unavailable") : t("empty")}
        </div>
      ) : navigation.url != null || document?.format === "html" ? (
        <iframe
          ref={frame}
          key={`${document?.version}:${navigation.generation}:${navigation.url}`}
          title={t("title")}
          src={navigation.url ?? `${documentUrl}?v=${document?.version}`}
          sandbox="allow-scripts allow-forms allow-popups"
          referrerPolicy="no-referrer"
          className={cn("w-full border-0 bg-white", MIN_H_0, FLEX_1)}
        />
      ) : (
        <div className={cn("chat-md overflow-auto p-5 text-sm", MIN_H_0, FLEX_1)}>
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
            a: ({ href, children }) => <a href={href} onClick={(event) => {
              if (!href || href.startsWith("#") || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
              if (!/^https?:\/\//i.test(href)) return;
              event.preventDefault();
              setNavigation((n) => ({ url: href, generation: n.generation + 1 }));
            }}>{children}</a>,
          }}>{content}</ReactMarkdown>
        </div>
      )}
    </section>
  );
}
