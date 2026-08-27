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

interface ExpectedText {
  type: "text";
  value: string;
}

interface ExpectedWrapper {
  type: "strong" | "emphasis";
  children: (ExpectedText | ExpectedLink)[];
}

type ExpectedChild =
  | ExpectedText
  | { type: "inlineCode"; value: string }
  | ExpectedLink
  | ExpectedWrapper;

const text = (value: string): ExpectedText => ({ type: "text", value });

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
): ExpectedWrapper => ({ type, children: [link(url, value)] });

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
  it("restores both strong spans in the reported message", () => {
    const markdown =
      "方案报告在 **http://100.103.96.72:8000/pages/3682-events-checkpoint-growth-p2/**，附三项需你决策的清单：**events drop / N-step canary / Loki archive 365d**——你有空看时拍板，不急。";

    const children = paragraphChildren(markdown);

    expect(children).toMatchObject([
      text("方案报告在 "),
      wrappedLink(
        "strong",
        "http://100.103.96.72:8000/pages/3682-events-checkpoint-growth-p2/",
      ),
      text("，附三项需你决策的清单："),
      {
        type: "strong",
        children: [
          text("events drop / N-step canary / Loki archive 365d"),
        ],
      },
      text("——你有空看时拍板，不急。"),
    ] satisfies ExpectedChild[]);
  });

  it("restores a non-adjacent strong opener before a labeled URL", () => {
    const children = paragraphChildren(
      "**Grafana：http://100.103.96.72:3003**（匿名 viewer，无需登录）",
    );

    expect(children).toMatchObject([
      {
        type: "strong",
        children: [
          text("Grafana："),
          link("http://100.103.96.72:3003"),
        ],
      },
      text("（匿名 viewer，无需登录）"),
    ] satisfies ExpectedChild[]);
  });

  it("restores a swallowed closer for a non-first link inside strong", () => {
    const children = paragraphChildren("**a http://x.com/p2/**，b**c**");

    expect(children).toMatchObject([
      text("，b"),
      {
        type: "strong",
        children: [
          text("a "),
          link("http://x.com/p2/"),
          text("c"),
        ],
      },
    ] satisfies ExpectedChild[]);
  });

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
