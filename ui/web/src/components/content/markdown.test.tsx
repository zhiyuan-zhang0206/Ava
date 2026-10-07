// ChatMarkdown: render + link safety (urlTransform blocks javascript:) +
// external links open new tab + fenced python block routes to PythonCode +
// inline code does not enter PythonCode.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// PythonCode mock — avoid running Prism syntax highlighting in tests
vi.mock("./python-code", () => ({
  PythonCode: ({ code }: { code: string }) => (
    <pre data-testid="python-code">{code}</pre>
  ),
}));

import { ChatMarkdown } from "./markdown";

afterEach(cleanup);

describe("ChatMarkdown", () => {
  it("renders plain text", () => {
    render(<ChatMarkdown content="hello world" />);
    expect(screen.getByText("hello world")).toBeTruthy();
  });

  it("external link (https://) auto-adds target=_blank rel=noopener noreferrer", () => {
    render(<ChatMarkdown content="[link](https://example.com)" />);
    const a = screen.getByRole("link", { name: "link" });
    expect(a.getAttribute("target")).toBe("_blank");
    expect(a.getAttribute("rel")).toContain("noopener");
    expect(a.getAttribute("href")).toBe("https://example.com");
  });

  it("ends a literal autolink before fullwidth punctuation and CJK text", () => {
    render(<ChatMarkdown content={"\u6d4f\u89c8\u5668\u5f00 https://ip.sb\uff1a\u663e\u793a"} />);

    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(1);
    const link = screen.getByRole("link", { name: "https://ip.sb" });
    expect(link.getAttribute("href")).toBe("https://ip.sb");
    expect(link.nextSibling?.textContent).toBe("\uff1a\u663e\u793a");
  });

  it("keeps a CJK period outside a literal autolink", () => {
    render(<ChatMarkdown content={"https://ip.sb\u3002"} />);

    const link = screen.getByRole("link", { name: "https://ip.sb" });
    expect(link.getAttribute("href")).toBe("https://ip.sb");
    expect(link.nextSibling?.textContent).toBe("\u3002");
  });

  it("renders a CJK-bounded literal URL inside strong", () => {
    const { container } = render(
      <ChatMarkdown content={"**http://127.0.0.1:18025**\u3002"} />,
    );

    const link = container.querySelector("strong > a");
    expect(link?.getAttribute("href")).toBe("http://127.0.0.1:18025");
    expect(link?.textContent).toBe("http://127.0.0.1:18025");
    expect(link?.parentElement?.nextSibling?.textContent).toBe("\u3002");
  });

  it("renders a letter-bounded literal URL inside strong", () => {
    const { container } = render(
      <ChatMarkdown content="**http://127.0.0.1:18025**x" />,
    );

    const link = container.querySelector("strong > a");
    expect(link?.getAttribute("href")).toBe("http://127.0.0.1:18025");
    expect(link?.textContent).toBe("http://127.0.0.1:18025");
    expect(link?.parentElement?.nextSibling?.textContent).toBe("x");
  });

  it("renders the reported bold URL without swallowed delimiters", () => {
    const content =
      "\u65b9\u6848\u62a5\u544a\u5728 **http://10.0.0.72:8000/pages/3682-events-checkpoint-growth-p2/**\uff0c\u9644\u4e09\u9879\u9700\u4f60\u51b3\u7b56\u7684\u6e05\u5355\uff1a**events drop / N-step canary / Loki archive 365d**——\u4f60\u6709\u7a7a\u770b\u65f6\u62cd\u677f\uff0c\u4e0d\u6025\u3002";
    const { container } = render(<ChatMarkdown content={content} />);

    const link = container.querySelector("strong > a");
    expect(link?.getAttribute("href")).toBe(
      "http://10.0.0.72:8000/pages/3682-events-checkpoint-growth-p2/",
    );
    expect(link?.textContent).not.toContain("**");
    expect(container.textContent).not.toContain("**");
  });

  it("leaves delimiter characters in a bare literal URL", () => {
    const { container } = render(
      <ChatMarkdown content="http://example.com/a**b" />,
    );

    expect(container.querySelector("strong")).toBeNull();
    const link = container.querySelector("a");
    expect(link?.getAttribute("href")).toBe("http://example.com/a**b");
    expect(link?.textContent).toBe("http://example.com/a**b");
  });

  it("renders a literal URL with query parameters inside strong", () => {
    const { container } = render(
      <ChatMarkdown content={"**https://example.com/a?b=c&d=1**\u3002"} />,
    );

    const link = container.querySelector("strong > a");
    expect(link?.getAttribute("href")).toBe(
      "https://example.com/a?b=c&d=1",
    );
    expect(link?.textContent).toBe("https://example.com/a?b=c&d=1");
    expect(link?.parentElement?.nextSibling?.textContent).toBe("\u3002");
  });

  it("keeps a Unicode ellipsis outside a literal autolink", () => {
    render(<ChatMarkdown content="https://ip.sb……" />);

    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(1);
    const link = screen.getByRole("link", { name: "https://ip.sb" });
    expect(link.getAttribute("href")).toBe("https://ip.sb");
    expect(link.nextSibling?.textContent).toBe("……");
  });

  it("same-page anchor (#foo) does not add target=_blank", () => {
    render(<ChatMarkdown content="[a](#foo)" />);
    const a = screen.getByRole("link", { name: "a" });
    expect(a.getAttribute("target")).toBeNull();
    expect(a.getAttribute("href")).toBe("#foo");
  });

  it("javascript: URL rewritten to about:blank by urlTransform (XSS guard)", () => {
    render(
      <ChatMarkdown content="[evil](javascript:alert(1))" />,
    );
    const a = screen.getByRole("link", { name: "evil" });
    expect(a.getAttribute("href")).toBe("about:blank");
  });

  it("fenced ```python``` block routes to PythonCode component", () => {
    render(<ChatMarkdown content={"```python\nprint('hi')\n```"} />);
    expect(screen.getByTestId("python-code").textContent).toBe("print('hi')");
  });

  it("inline `code` does not route to PythonCode (no language-* className)", () => {
    render(<ChatMarkdown content="some `inline` code" />);
    expect(screen.queryByTestId("python-code")).toBeNull();
    // inline <code> in the DOM
    const codes = document.querySelectorAll("code");
    expect(codes.length).toBeGreaterThan(0);
  });

  it("non-python fenced block uses plain <pre><code>", () => {
    render(<ChatMarkdown content={"```bash\necho hi\n```"} />);
    expect(screen.queryByTestId("python-code")).toBeNull();
    const pre = document.querySelector("pre");
    expect(pre).toBeTruthy();
    expect(pre?.textContent).toBe("echo hi");
  });

  // Regression: a bare ``` fence with no language carries no language-*
  // className, so the old "language-* ? block : inline" rule rendered it as
  // inline <code> — collapsing newlines and mangling multi-line content
  // (e.g. an ASCII diagram). It must render as a <pre> block.
  it("no-language fence renders as a <pre> block, not inline (newlines kept)", () => {
    render(<ChatMarkdown content={"```\nline1\nline2\n```"} />);
    expect(screen.queryByTestId("python-code")).toBeNull();
    const pre = document.querySelector("pre");
    expect(pre).toBeTruthy();
    // textContent keeps the newline between lines (pre preserves whitespace)
    expect(pre?.textContent).toBe("line1\nline2");
  });

  it("multi-line ASCII diagram in a bare fence stays a <pre> block", () => {
    const diagram = "```\n┌──┐\n│\u4f60│\n└──┘\n```";
    render(<ChatMarkdown content={diagram} />);
    const pre = document.querySelector("pre");
    expect(pre).toBeTruthy();
    expect(pre?.textContent).toContain("┌──┐");
    expect(pre?.textContent).toContain("\n");
  });

  it("inline `code` with no newline still renders inline (not a <pre>)", () => {
    render(<ChatMarkdown content="some `inline` code" />);
    // The only <code> is inline — no <pre> wrapper around it.
    const inlineCode = screen.getByText("inline");
    expect(inlineCode.tagName).toBe("CODE");
    expect(inlineCode.closest("pre")).toBeNull();
  });
});


// CJK emphasis regression (task #3314): CommonMark's flanking rule treats a
// punctuation run right after the opening `**` as blocking left-flanking
// when it follows a CJK letter, so `**"..."**` stayed plain text. remark-
// cjk-friendly (+ the gfm-strikethrough companion, required while remark-gfm
// is in the pipeline) restores emphasis for CJK-adjacent delimiters. Keep
// the reported case, its quote variants, the strikethrough family, and the
// plain/ASCII controls together — this file is the markdown contract.
describe("CJK emphasis (remark-cjk-friendly, task #3314)", () => {
  it("renders bold for the reported sentence (curly quotes)", () => {
    const { container } = render(
      <ChatMarkdown
        content={"\u4e0d\u7b97\u5168\u9a97\u5c40\uff0c\u4f46**\u201c\u7f8e\u767d\u201d\u8fd9\u4e2a\u8bcd\u88ab\u7528\u6b6a\u4e86**\u3002\u62c6\u5f00\u770b\u5c31\u6e05\u695a\u4e86\uff1a"}
      />,
    );
    const strong = container.querySelector("strong");
    expect(strong?.textContent).toBe(
      "\u201c\u7f8e\u767d\u201d\u8fd9\u4e2a\u8bcd\u88ab\u7528\u6b6a\u4e86",
    );
  });

  it("renders bold with straight ASCII quotes", () => {
    const { container } = render(
      <ChatMarkdown content={"\u4f46**\"\u7f8e\u767d\"\u8fd9\u4e2a\u8bcd**\u3002"} />,
    );
    expect(container.querySelector("strong")?.textContent).toBe(
      "\"\u7f8e\u767d\"\u8fd9\u4e2a\u8bcd",
    );
  });

  it("renders bold with corner brackets", () => {
    const { container } = render(
      <ChatMarkdown content={"\u4f46**\u300c\u7f8e\u767d\u300d\u8fd9\u4e2a\u8bcd**\u3002"} />,
    );
    expect(container.querySelector("strong")?.textContent).toBe(
      "\u300c\u7f8e\u767d\u300d\u8fd9\u4e2a\u8bcd",
    );
  });

  it("renders strikethrough for quoted CJK (GFM companion)", () => {
    const { container } = render(
      <ChatMarkdown content={"\u4f46~~\u201c\u7f8e\u767d\u201d\u8fd9\u4e2a\u8bcd~~\u3002"} />,
    );
    expect(container.querySelector("del")?.textContent).toBe(
      "\u201c\u7f8e\u767d\u201d\u8fd9\u4e2a\u8bcd",
    );
  });

  it("leaves plain CJK and ASCII emphasis unchanged", () => {
    const { container } = render(
      <ChatMarkdown content={"\u4f46**\u7f8e\u767d**\u8fd9\u4e2a\u8bcd\u3002 word**bold**word."} />,
    );
    const strongs = container.querySelectorAll("strong");
    expect(strongs).toHaveLength(2);
    expect(strongs[0].textContent).toBe("\u7f8e\u767d");
    expect(strongs[1].textContent).toBe("bold");
  });
});

// Single-tilde safety (task #3653): GFM strikethrough with the default
// singleTilde paired the `~61G` and `~/work` of one sentence and struck the
// text between them (user report). A lone `~` must stay literal; only the
// double form may strike.
describe("single tilde stays literal (task #3653)", () => {
  it("renders the reported sentence without strikethrough", () => {
    const { container } = render(
      <ChatMarkdown content={"57 \u4e2a worktree \u5171 ~61G\u3002\u7ee7\u7eed\u76d8\u70b9\uff1aworktree \u7684\u5f52\u5c5e repo\u3001~/work \u5176\u4f59\u73af\u5883\u3001\u4ee5\u53ca Group Containers/eagle-local \u7b49\u533a\u57df\u3002"} />,
    );
    expect(container.querySelector("del")).toBeNull();
    expect(container.textContent).toContain("~61G");
    expect(container.textContent).toContain("~/work");
  });

  it("keeps a lone ~ pair literal in CJK text", () => {
    const { container } = render(
      <ChatMarkdown content={"\u4e2d\u6587 ~x~ \u4e2d\u6587"} />,
    );
    expect(container.querySelector("del")).toBeNull();
    expect(container.textContent).toContain("~x~");
  });

  it("keeps ~~...~~ strikethrough, including emoji-adjacent", () => {
    const { container } = render(
      <ChatMarkdown content={"~~\u5b8c\u6210~~\u2705 \u548c \u2705~~\u5b8c\u6210~~"} />,
    );
    const dels = container.querySelectorAll("del");
    expect(dels).toHaveLength(2);
    expect(dels[0].textContent).toBe("\u5b8c\u6210");
  });

  it("only the double form strikes in a mixed line", () => {
    const { container } = render(
      <ChatMarkdown content={"~~a~~ \u548c ~b~ \u6df7\u5408"} />,
    );
    const dels = container.querySelectorAll("del");
    expect(dels).toHaveLength(1);
    expect(dels[0].textContent).toBe("a");
  });

  it("leaves tildes inside inline code alone", () => {
    const { container } = render(
      <ChatMarkdown content={"`~/work` \u548c `~61G` \u548c `~/.ava`"} />,
    );
    expect(container.querySelector("del")).toBeNull();
    const codes = container.querySelectorAll("code");
    expect(codes).toHaveLength(3);
    expect(Array.from(codes).map((c) => c.textContent).join("|")).toBe(
      "~/work|~61G|~/.ava",
    );
  });
});
