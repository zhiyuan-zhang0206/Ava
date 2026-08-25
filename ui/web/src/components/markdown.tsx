"use client";

import { memo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { CopyButton } from "@/components/copy-button";
import { PythonCode } from "@/components/python-code";
import { MIN_W_0 } from "@/lib/layout";
import remarkAutolinkDelimiter from "@/lib/remark-autolink-delimiter";
import remarkCjkLinkBoundary from "@/lib/remark-cjk-link-boundary";
import { cn } from "@/lib/utils";

// Only used for chat items. user / info / error stay as plain text.
//
// Security:
// - rehype-raw is disabled → any raw HTML (e.g. <script>) is treated as text
// - urlTransform only allows http(s) / mailto / same-page #anchor; other
//   schemes like javascript: return "about:blank" so the link is broken
//   but still visible (XSS guard)
// - external links (http/https) automatically get target="_blank"
//   rel="noopener noreferrer"; same-page anchors don't open a new tab
//
// Known limitation: urlTransform also applies to other URL attributes
// like <img src>. The component doesn't override <img> right now, so
// agent output using ![](relative.png) or data:image/png;... gets
// rewritten to about:blank and renders as a broken image. Agent output
// rarely embeds markdown images, so leave it for now; when needed,
// override <img> and add data:image/ to the urlTransform allowlist.

const urlTransform = (url: string): string => {
  return /^(https?:|mailto:|#)/i.test(url) ? url : "about:blank";
};

const components: Components = {
  // Strip react-markdown's default <pre> wrapper around fenced code —
  // inner PythonCode / plain fenced already carry their own <pre>;
  // without stripping you get nested <pre><pre> (invalid HTML +
  // .chat-md pre double-layer bg).
  pre: ({ node: _node, children }) => <>{children}</>,
  a: ({ node: _node, href, ...props }) => {
    const external = /^https?:/i.test(href ?? "");
    return external ? (
      <a href={href} {...props} target="_blank" rel="noopener noreferrer" />
    ) : (
      <a href={href} {...props} />
    );
  },
  code: ({ node: _node, className, children, ...props }) => {
    // react-markdown v10: one `code` component for both inline and block.
    // A fenced block tagged ```lang carries className="language-lang", but a
    // bare ``` with no language carries no className — so language presence
    // alone can't tell inline from block, and a no-language fence would fall
    // through to the inline branch, collapsing its newlines (an ASCII
    // diagram renders as one wrapped line). Inline code never spans lines;
    // a fenced block's content always carries a trailing newline. Treat
    // "contains a newline" as the block signal so a no-language fence keeps
    // its <pre> whitespace.
    const lang = /language-(\w+)/.exec(className ?? "")?.[1];
    // eslint-disable-next-line @typescript-eslint/no-base-to-string -- ReactNode → string for code highlighting
    const text = String(children);
    if (lang === undefined && !text.includes("\n")) {
      // inline code
      return (
        <code className={className} {...props}>
          {children}
        </code>
      );
    }
    const codeStr = text.replace(/\n$/, "");
    if (lang === "python") {
      return <PythonCode code={codeStr} />;
    }
    // Block, language-tagged or bare ``` fence: wrap in a group-relative
    // container with a CopyButton. The inner <pre> already gets my-2 from
    // .chat-md CSS, so we don't add extra margin on the wrapper.
    return (
      <div className="group relative">
        <CopyButton text={codeStr} label={lang ? `${lang} code` : "code"} />
        <pre>
          <code className={className}>{codeStr}</code>
        </pre>
      </div>
    );
  },
};

interface Props {
  content: string;
}

// memo: during streaming every chat_delta triggers a full timeline
// re-render, but the content prop ChatMarkdown receives only changes
// when its own item accumulates. The default shallow comparator is
// enough for a string prop — sibling ChatMarkdowns hit memo and skip
// re-parsing through ReactMarkdown (remarkGfm tables / strikethrough
// parsing is non-trivial for multi-kilobyte markdown).
export const ChatMarkdown = memo(function ChatMarkdown({ content }: Props) {
  return (
    <div className={cn("chat-md font-sans", MIN_W_0)}>
      <ReactMarkdown
        remarkPlugins={[
          remarkGfm,
          remarkCjkLinkBoundary,
          remarkAutolinkDelimiter,
        ]}
        urlTransform={urlTransform}
        components={components}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
});
