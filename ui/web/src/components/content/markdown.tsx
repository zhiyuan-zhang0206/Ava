// meiva:note
// ChatMarkdown is a lazy boundary: the renderer (the react-markdown chain) is a deferred chunk, so route bundles carry only the boundary and a cold first paint shows the same content as plain text inside the shared chat-md wrapper. A resolved renderer renders Markdown on the first paint; a pending one upgrades the mounted tree when it arrives, and a superseded or unmounted attempt never writes back. Prefetch starts when the boundary module evaluates and is shared and deduplicated by one module-level promise; a rejected load clears the cache, keeps the fallback, and the next attempt retries.
// meiva:link ui/web/src/docs/frontend-components/markdown-surfaces.meiva.okf.md
"use client";

import { memo, useEffect, useState } from "react";

import type { ChatMarkdownProps, ChatMarkdownRenderer } from "@/components/content/markdown-renderer";
import { cn } from "@/lib/format/utils";
import { MIN_W_0 } from "@/lib/layout/layout";

type Renderer = typeof ChatMarkdownRenderer;

let importRenderer: () => Promise<Renderer> = () =>
  import("@/components/content/markdown-renderer").then((module) => module.ChatMarkdownRenderer);
let rendererPromise: Promise<Renderer> | null = null;
let resolvedRenderer: Renderer | null = null;

function loadChatMarkdownRenderer(): Promise<Renderer> {
  if (rendererPromise !== null) return rendererPromise;
  const promise = importRenderer()
    .then((Renderer) => {
      if (rendererPromise === promise) resolvedRenderer = Renderer;
      return Renderer;
    })
    .catch((error: unknown) => {
      if (rendererPromise === promise) rendererPromise = null;
      throw error;
    });
  rendererPromise = promise;
  return promise;
}

export function preloadChatMarkdown(): Promise<void> {
  return loadChatMarkdownRenderer().then(
    () => undefined,
    () => undefined,
  );
}

if (typeof window !== "undefined") {
  void preloadChatMarkdown();
}

export const ChatMarkdown = memo(function ChatMarkdown(props: ChatMarkdownProps) {
  const [Renderer, setRenderer] = useState<Renderer | null>(() => resolvedRenderer);

  useEffect(() => {
    if (Renderer !== null) return;
    let active = true;
    void loadChatMarkdownRenderer().then(
      (Renderer) => {
        if (active) setRenderer(() => Renderer);
      },
      () => undefined,
    );
    return () => {
      active = false;
    };
  }, [Renderer]);

  if (Renderer === null) return <PlainChatMarkdown content={props.content} />;
  return <Renderer {...props} />;
});

function PlainChatMarkdown({ content }: { content: string }) {
  return (
    <div className={cn("chat-md font-sans", MIN_W_0)} data-testid="chat-markdown-plain">
      <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">{content}</p>
    </div>
  );
}

export function __resetMarkdownCacheForTests(): void {
  rendererPromise = null;
  resolvedRenderer = null;
}

export function __setMarkdownImportForTests(loader: (() => Promise<Renderer>) | null): void {
  importRenderer =
    loader ?? (() => import("@/components/content/markdown-renderer").then((module) => module.ChatMarkdownRenderer));
}
