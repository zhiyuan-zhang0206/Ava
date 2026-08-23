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
      markdown: "https://ip.sb：显示",
      expected: [link("https://ip.sb"), text("：显示")],
    },
    {
      name: "splits at a CJK period",
      markdown: "https://ip.sb。",
      expected: [link("https://ip.sb"), text("。")],
    },
    {
      name: "splits at a fullwidth comma",
      markdown: "https://ip.sb，再见",
      expected: [link("https://ip.sb"), text("，再见")],
    },
    {
      name: "splits at a Unicode ellipsis",
      markdown: "https://ip.sb……",
      expected: [link("https://ip.sb"), text("……")],
    },
    {
      name: "splits at an em dash",
      markdown: "https://ip.sb——说明",
      expected: [link("https://ip.sb"), text("——说明")],
    },
    {
      name: "merges the remainder with following text",
      markdown: "https://ip.sb： 显示",
      expected: [link("https://ip.sb"), text("： 显示")],
    },
    {
      name: "preserves query parameters before a CJK boundary",
      markdown: "https://ip.sb?a=1&b=2。",
      expected: [link("https://ip.sb?a=1&b=2"), text("。")],
    },
    {
      name: "splits at Han text without intervening punctuation",
      markdown: "https://ip.sb中文",
      expected: [link("https://ip.sb"), text("中文")],
    },
    {
      name: "splits at fullwidth parentheses",
      markdown: "https://ip.sb（备注）",
      expected: [link("https://ip.sb"), text("（备注）")],
    },
    {
      name: "maps a www boundary past the inferred scheme",
      markdown: "www.ip.sb：显示",
      expected: [link("http://www.ip.sb", "www.ip.sb"), text("：显示")],
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
      markdown: "<https://ip.sb：显示>",
      expected: [link("https://ip.sb：显示")],
    },
    {
      name: "leaves an explicit link unchanged",
      markdown: "[点击](https://ip.sb：显示)",
      expected: [link("https://ip.sb：显示", "点击")],
    },
    {
      name: "keeps surrounding text in one clean remainder node",
      markdown: "前 https://ip.sb：显示 后",
      expected: [text("前 "), link("https://ip.sb"), text("：显示 后")],
    },
    {
      name: "leaves an ASCII URL unchanged",
      markdown: "https://example.com/path?q=hello#frag",
      expected: [link("https://example.com/path?q=hello#frag")],
    },
    {
      name: "continues walking after splitting an earlier link",
      markdown: "看 https://a.com。 和 https://b.com",
      expected: [
        text("看 "),
        link("https://a.com"),
        text("。 和 "),
        link("https://b.com"),
      ],
    },
  ])("$name", ({ markdown, expected }) => {
    const children = paragraphChildren(markdown);

    expect(children).toHaveLength(expected.length);
    expect(children).toMatchObject(expected);
  });
});
