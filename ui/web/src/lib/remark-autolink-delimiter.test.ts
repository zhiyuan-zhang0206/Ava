import type { PhrasingContent, Root } from "mdast";
import remarkGfm from "remark-gfm";
import remarkParse from "remark-parse";
import { unified } from "unified";
import { describe, expect, it } from "vitest";

import remarkAutolinkDelimiter from "./remark-autolink-delimiter";
import remarkCjkLinkBoundary from "./remark-cjk-link-boundary";

interface ExpectedLink {
  type: "link";
  url: string;
  title: null;
  children: [{ type: "text"; value: string }];
}

type ExpectedChild =
  | { type: "text"; value: string }
  | { type: "inlineCode"; value: string }
  | ExpectedLink
  | {
      type: "strong" | "emphasis";
      children: [ExpectedLink];
    };

const text = (value: string): ExpectedChild => ({ type: "text", value });

const link = (url: string, value = url): ExpectedLink => ({
  type: "link",
  url,
  title: null,
  children: [{ type: "text", value }],
});

const wrappedLink = (
  type: "strong" | "emphasis",
  url: string,
  value = url,
): ExpectedChild => ({ type, children: [link(url, value)] });

function paragraphChildren(markdown: string): PhrasingContent[] {
  const processor = unified()
    .use(remarkParse)
    .use(remarkGfm)
    .use(remarkCjkLinkBoundary)
    .use(remarkAutolinkDelimiter);
  const tree = processor.runSync(processor.parse(markdown), markdown) as Root;
  if (tree.children.length !== 1) {
    throw new Error("Expected one paragraph");
  }
  const paragraph = tree.children[0];
  if (paragraph.type !== "paragraph") {
    throw new Error("Expected one paragraph");
  }
  return paragraph.children;
}

describe("remarkAutolinkDelimiter", () => {
  it.each<{
    name: string;
    markdown: string;
    expected: ExpectedChild[];
  }>([
    {
      name: "restores strong around a literal URL before a CJK period",
      markdown: "**http://127.0.0.1:18025**。",
      expected: [
        wrappedLink("strong", "http://127.0.0.1:18025"),
        text("。"),
      ],
    },
    {
      name: "restores strong around a literal URL before an ASCII letter",
      markdown: "**http://127.0.0.1:18025**x",
      expected: [
        wrappedLink("strong", "http://127.0.0.1:18025"),
        text("x"),
      ],
    },
    {
      name: "restores emphasis around a literal URL",
      markdown: "*http://example.com*x",
      expected: [wrappedLink("emphasis", "http://example.com"), text("x")],
    },
    {
      name: "maps a www literal past its inferred scheme",
      markdown: "**www.example.com**x",
      expected: [
        wrappedLink("strong", "http://www.example.com", "www.example.com"),
        text("x"),
      ],
    },
    {
      name: "leaves correctly parsed strong autolinks unchanged",
      markdown: "前 **http://127.0.0.1:18025** 后",
      expected: [
        text("前 "),
        wrappedLink("strong", "http://127.0.0.1:18025"),
        text(" 后"),
      ],
    },
    {
      name: "leaves a bare URL containing delimiters unchanged",
      markdown: "http://example.com/a**b",
      expected: [link("http://example.com/a**b")],
    },
    {
      name: "leaves a CommonMark full autolink unchanged",
      markdown: "<https://ip.sb：显示>",
      expected: [link("https://ip.sb：显示")],
    },
    {
      name: "leaves an explicit link unchanged",
      markdown: "[x](https://a.com)",
      expected: [link("https://a.com", "x")],
    },
    {
      name: "leaves a non-emphasized CJK-bounded literal link unchanged",
      markdown: "https://ip.sb。",
      expected: [link("https://ip.sb"), text("。")],
    },
    {
      name: "leaves inline code unchanged",
      markdown: "`**http://example.com**`",
      expected: [{ type: "inlineCode", value: "**http://example.com**" }],
    },
  ])("$name", ({ markdown, expected }) => {
    const children = paragraphChildren(markdown);

    expect(children).toHaveLength(expected.length);
    expect(children).toMatchObject(expected);
  });
});
