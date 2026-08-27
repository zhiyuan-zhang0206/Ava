import type { PhrasingContent, Root } from "mdast";
import remarkGfm from "remark-gfm";
import remarkParse from "remark-parse";
import { unified } from "unified";
import { describe, expect, it } from "vitest";

import remarkCjkLinkBoundary from "./remark-cjk-link-boundary";

type ExpectedChild =
  | { type: "text"; value: string }
  | {
      type: "link";
      url: string;
      title: null;
      children: [{ type: "text"; value: string }];
    };

const text = (value: string): ExpectedChild => ({ type: "text", value });

const link = (url: string, value = url): ExpectedChild => ({
  type: "link",
  url,
  title: null,
  children: [{ type: "text", value }],
});

function paragraphChildren(markdown: string): PhrasingContent[] {
  const processor = unified()
    .use(remarkParse)
    .use(remarkGfm)
    .use(remarkCjkLinkBoundary);
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

describe("remarkCjkLinkBoundary", () => {
  it.each<{
    name: string;
    markdown: string;
    expected: ExpectedChild[];
  }>([
    {
      name: "splits at a fullwidth colon",
      markdown: "https://ip.sb\uff1a\u663e\u793a",
      expected: [link("https://ip.sb"), text("\uff1a\u663e\u793a")],
    },
    {
      name: "splits at a CJK period",
      markdown: "https://ip.sb\u3002",
      expected: [link("https://ip.sb"), text("\u3002")],
    },
    {
      name: "splits at a fullwidth comma",
      markdown: "https://ip.sb\uff0c\u518d\u89c1",
      expected: [link("https://ip.sb"), text("\uff0c\u518d\u89c1")],
    },
    {
      name: "splits at a Unicode ellipsis",
      markdown: "https://ip.sb……",
      expected: [link("https://ip.sb"), text("……")],
    },
    {
      name: "splits at an em dash",
      markdown: "https://ip.sb——\u8bf4\u660e",
      expected: [link("https://ip.sb"), text("——\u8bf4\u660e")],
    },
    {
      name: "merges the remainder with following text",
      markdown: "https://ip.sb\uff1a \u663e\u793a",
      expected: [link("https://ip.sb"), text("\uff1a \u663e\u793a")],
    },
    {
      name: "preserves query parameters before a CJK boundary",
      markdown: "https://ip.sb?a=1&b=2\u3002",
      expected: [link("https://ip.sb?a=1&b=2"), text("\u3002")],
    },
    {
      name: "splits at Han text without intervening punctuation",
      markdown: "https://ip.sb\u4e2d\u6587",
      expected: [link("https://ip.sb"), text("\u4e2d\u6587")],
    },
    {
      name: "splits at fullwidth parentheses",
      markdown: "https://ip.sb\uff08\u5907\u6ce8\uff09",
      expected: [link("https://ip.sb"), text("\uff08\u5907\u6ce8\uff09")],
    },
    {
      name: "maps a www boundary past the inferred scheme",
      markdown: "www.ip.sb\uff1a\u663e\u793a",
      expected: [link("http://www.ip.sb", "www.ip.sb"), text("\uff1a\u663e\u793a")],
    },
    {
      name: "keeps GFM ASCII trailing-punctuation behavior",
      markdown: "https://ip.sb.",
      expected: [link("https://ip.sb"), text(".")],
    },
    {
      name: "keeps an ASCII comma inside a path",
      markdown: "https://ip.sb/a,b",
      expected: [link("https://ip.sb/a,b")],
    },
    {
      name: "leaves a CommonMark full autolink unchanged",
      markdown: "<https://ip.sb\uff1a\u663e\u793a>",
      expected: [link("https://ip.sb\uff1a\u663e\u793a")],
    },
    {
      name: "leaves an explicit link unchanged",
      markdown: "[\u70b9\u51fb](https://ip.sb\uff1a\u663e\u793a)",
      expected: [link("https://ip.sb\uff1a\u663e\u793a", "\u70b9\u51fb")],
    },
    {
      name: "keeps surrounding text in one clean remainder node",
      markdown: "\u524d https://ip.sb\uff1a\u663e\u793a \u540e",
      expected: [text("\u524d "), link("https://ip.sb"), text("\uff1a\u663e\u793a \u540e")],
    },
    {
      name: "leaves an ASCII URL unchanged",
      markdown: "https://example.com/path?q=hello#frag",
      expected: [link("https://example.com/path?q=hello#frag")],
    },
    {
      name: "continues walking after splitting an earlier link",
      markdown: "\u770b https://a.com\u3002 \u548c https://b.com",
      expected: [
        text("\u770b "),
        link("https://a.com"),
        text("\u3002 \u548c "),
        link("https://b.com"),
      ],
    },
  ])("$name", ({ markdown, expected }) => {
    const children = paragraphChildren(markdown);

    expect(children).toHaveLength(expected.length);
    expect(children).toMatchObject(expected);
  });
});
