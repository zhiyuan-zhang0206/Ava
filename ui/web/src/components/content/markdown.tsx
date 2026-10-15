// ava:ref ui/web/src/docs/frontend-components/markdown-surfaces.ava.okf.md#markdown-rendering
"use client";

import { memo } from "react";
import ReactMarkdown, { type Components, type UrlTransform } from "react-markdown";
import remarkCjkFriendly from "remark-cjk-friendly";
import remarkCjkFriendlyGfmStrikethrough from "remark-cjk-friendly-gfm-strikethrough";
import remarkGfm from "remark-gfm";

import { CopyButton } from "@/components/content/copy-button";
import { PythonCode } from "@/components/content/python-code";
import { MIN_W_0 } from "@/lib/layout/layout";
import remarkAutolinkDelimiter from "@/lib/markdown/remark-autolink-delimiter";
import remarkCjkLinkBoundary from "@/lib/markdown/remark-cjk-link-boundary";
import { cn } from "@/lib/format/utils";

const urlTransform = (url: string): string => {
  return /^(https?:|mailto:|#)/i.test(url) ? url : "about:blank";
};

const components: Components = {

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

    const lang = /language-(\w+)/.exec(className ?? "")?.[1];

    const text = typeof children === "string" ? children : "";
    if (lang === undefined && !text.includes("\n")) {

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
  components?: Components;
  resolveUrl?: UrlTransform;
  skipHtml?: boolean;
}

export const ChatMarkdown = memo(function ChatMarkdown({ content, components: overrides, resolveUrl, skipHtml }: Props) {
  return (
    <div className={cn("chat-md font-sans", MIN_W_0)}>
      <ReactMarkdown
        remarkPlugins={[

          [remarkGfm, { singleTilde: false }],

          remarkCjkFriendly,
          [remarkCjkFriendlyGfmStrikethrough, { singleTilde: false }],
          remarkCjkLinkBoundary,
          remarkAutolinkDelimiter,
        ]}
        urlTransform={resolveUrl ?? urlTransform}
        components={overrides ? { ...components, ...overrides } : components}
        skipHtml={skipHtml}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
});
