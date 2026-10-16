// ava:ref ui/web/src/docs/frontend-components/markdown-surfaces.ava.okf.md#markdown-rendering
"use client";

import { memo, useEffect, useState } from "react";
import ReactMarkdown, { type Components, type UrlTransform } from "react-markdown";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import rehypeSlug from "rehype-slug";
import remarkCjkFriendly from "remark-cjk-friendly";
import remarkCjkFriendlyGfmStrikethrough from "remark-cjk-friendly-gfm-strikethrough";
import remarkGfm from "remark-gfm";
import type { Plugin, PluggableList } from "unified";

import { CopyButton } from "@/components/content/copy-button";
import { PythonCode } from "@/components/content/python-code";
import { MIN_W_0 } from "@/lib/layout/layout";
import { loadMarkdownMath, type MarkdownMath } from "@/lib/markdown/math";
import remarkAutolinkDelimiter from "@/lib/markdown/remark-autolink-delimiter";
import remarkCjkLinkBoundary from "@/lib/markdown/remark-cjk-link-boundary";
import { cn } from "@/lib/format/utils";

const urlTransform = (url: string): string => {
  return /^(https?:|mailto:|#)/i.test(url) ? url : "about:blank";
};

// ava:ref ui/web/src/docs/frontend-components/markdown-safe-content.ava.okf.md#markdown-html-subset
const sanitizeSchema = {
  ...defaultSchema,
  clobberPrefix: "",
  strip: [...(defaultSchema.strip ?? []), "style", "iframe", "template"],
  tagNames: [...(defaultSchema.tagNames ?? []), "figure", "figcaption", "mark"],
  attributes: {
    ...defaultSchema.attributes,
    code: [...(defaultSchema.attributes?.code ?? []), ["className", "math-inline", "math-display"]],
  },
};

const baseRemarkPlugins: PluggableList = [
  [remarkGfm, { singleTilde: false }],
  remarkCjkFriendly,
  [remarkCjkFriendlyGfmStrikethrough, { singleTilde: false }],
  remarkCjkLinkBoundary,
  remarkAutolinkDelimiter,
];

interface HastLike {
  type: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: HastLike[];
}

type UrlResolver = (url: string) => string | null | undefined;

const rehypeResolveUrls: Plugin<[UrlResolver]> = (resolve) => (tree) => {
  const walk = (node: HastLike): void => {
    for (const child of node.children ?? []) walk(child);
    if (node.type !== "element" || node.properties === undefined) return;
    const href = node.properties.href;
    const src = node.properties.src;
    if (node.tagName === "a" && typeof href === "string" && !href.startsWith("#")) {
      node.properties.href = resolve(href);
    }
    if (node.tagName === "img" && typeof src === "string") {
      node.properties.src = resolve(src);
    }
  };
  walk(tree);
};

const baseRehypePlugins = (resolve: UrlTransform): PluggableList => [
  rehypeRaw,
  [rehypeResolveUrls, resolve],
  [rehypeSanitize, sanitizeSchema],
  rehypeSlug,
];

const components: Components = {
  pre: ({ node: _node, children }) => <>{children}</>,
  a: ({ node: _node, href, children, ...props }) => {
    const external = /^https?:/i.test(href ?? "");
    return external ? (
      <a href={href} {...props} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    ) : (
      <a href={href} {...props}>
        {children}
      </a>
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

export interface ChatMarkdownProps {
  content: string;
  components?: Components;
  resolveUrl?: UrlTransform;
}

// ava:ref ui/web/src/docs/frontend-components/markdown-safe-content.ava.okf.md#markdown-document-anchors
export const ChatMarkdownRenderer = memo(function ChatMarkdownRenderer({ content, components: overrides, resolveUrl }: ChatMarkdownProps) {
  const [math, setMath] = useState<MarkdownMath | null>(null);
  const needsMath = content.includes("$");
  useEffect(() => {
    if (!needsMath || math !== null) return;
    let active = true;
    void loadMarkdownMath().then(
      (loaded) => {
        if (active) setMath(loaded);
      },
      () => undefined,
    );
    return () => {
      active = false;
    };
  }, [needsMath, math]);

  return (
    <div className={cn("chat-md font-sans", MIN_W_0)}>
      <ReactMarkdown
        remarkPlugins={[...baseRemarkPlugins, ...(math?.remark ?? [])]}
        rehypePlugins={[...baseRehypePlugins(resolveUrl ?? urlTransform), ...(math?.rehype ?? [])]}
        urlTransform={resolveUrl ?? urlTransform}
        components={overrides ? { ...components, ...overrides } : components}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
});
