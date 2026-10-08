"use client";

// Markdown inside the details panel: the app's one renderer (`ChatMarkdown`) at the panel's
// type scale, so headings do not outgrow the panel's own titles.

import { ChatMarkdown } from "@/components/content/markdown";

export function TimelineMarkdown({ content, testId }: { content: string; testId?: string }) {
  return (
    <div
      className="text-sm leading-5 [&_.chat-md_h1]:text-sm [&_.chat-md_h2]:text-[13px] [&_.chat-md_h3]:text-xs [&_.chat-md_h3]:font-semibold"
      data-testid={testId}
    >
      <ChatMarkdown content={content} />
    </div>
  );
}
