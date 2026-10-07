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
      "\u65b9\u6848\u62a5\u544a\u5728 **http://10.0.0.72:8000/pages/3682-events-checkpoint-growth-p2/**\uff0c\u9644\u4e09\u9879\u9700\u4f60\u51b3\u7b56\u7684\u6e05\u5355\uff1a**events drop / N-step canary / Loki archive 365d**——\u4f60\u6709\u7a7a\u770b\u65f6\u62cd\u677f\uff0c\u4e0d\u6025\u3002";

    const children = paragraphChildren(markdown);

    expect(children).toMatchObject([
      text("\u65b9\u6848\u62a5\u544a\u5728 "),
      wrappedLink(
        "strong",
        "http://10.0.0.72:8000/pages/3682-events-checkpoint-growth-p2/",
      ),
      text("\uff0c\u9644\u4e09\u9879\u9700\u4f60\u51b3\u7b56\u7684\u6e05\u5355\uff1a"),
      {
        type: "strong",
        children: [
          text("events drop / N-step canary / Loki archive 365d"),
        ],
      },
      text("——\u4f60\u6709\u7a7a\u770b\u65f6\u62cd\u677f\uff0c\u4e0d\u6025\u3002"),
    ] satisfies ExpectedChild[]);
  });

  it("restores a non-adjacent strong opener before a labeled URL", () => {
    const children = paragraphChildren(
      "**Grafana\uff1ahttp://10.0.0.72:3003**\uff08\u533f\u540d viewer\uff0c\u65e0\u9700\u767b\u5f55\uff09",
    );

    expect(children).toMatchObject([
      {
        type: "strong",
        children: [
          text("Grafana\uff1a"),
          link("http://10.0.0.72:3003"),
        ],
      },
      text("\uff08\u533f\u540d viewer\uff0c\u65e0\u9700\u767b\u5f55\uff09"),
    ] satisfies ExpectedChild[]);
  });

  it("restores a swallowed closer for a non-first link inside strong", () => {
    const children = paragraphChildren("**a http://x.com/p2/**\uff0cb**c**");

    expect(children).toMatchObject([
      text("\uff0cb"),
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
      markdown: "**http://127.0.0.1:18025**\u3002",
      expected: [
        wrappedLink("strong", "http://127.0.0.1:18025"),
        text("\u3002"),
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
      markdown: "\u524d **http://127.0.0.1:18025** \u540e",
      expected: [
        text("\u524d "),
        wrappedLink("strong", "http://127.0.0.1:18025"),
        text(" \u540e"),
      ],
    },
    {
      name: "leaves a bare URL containing delimiters unchanged",
      markdown: "http://example.com/a**b",
      expected: [link("http://example.com/a**b")],
    },
    {
      name: "leaves a CommonMark full autolink unchanged",
      markdown: "<https://ip.sb\uff1a\u663e\u793a>",
      expected: [link("https://ip.sb\uff1a\u663e\u793a")],
    },
    {
      name: "leaves an explicit link unchanged",
      markdown: "[x](https://a.com)",
      expected: [link("https://a.com", "x")],
    },
    {
      name: "leaves a non-emphasized CJK-bounded literal link unchanged",
      markdown: "https://ip.sb\u3002",
      expected: [link("https://ip.sb"), text("\u3002")],
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
