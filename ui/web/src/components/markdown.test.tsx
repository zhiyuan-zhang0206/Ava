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
    render(<ChatMarkdown content="浏览器开 https://ip.sb：显示" />);

    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(1);
    const link = screen.getByRole("link", { name: "https://ip.sb" });
    expect(link.getAttribute("href")).toBe("https://ip.sb");
    expect(link.nextSibling?.textContent).toBe("：显示");
  });

  it("keeps a CJK period outside a literal autolink", () => {
    render(<ChatMarkdown content="https://ip.sb。" />);

    const link = screen.getByRole("link", { name: "https://ip.sb" });
    expect(link.getAttribute("href")).toBe("https://ip.sb");
    expect(link.nextSibling?.textContent).toBe("。");
  });

  it("renders a CJK-bounded literal URL inside strong", () => {
    const { container } = render(
      <ChatMarkdown content="**http://127.0.0.1:18025**。" />,
    );

    const link = container.querySelector("strong > a");
    expect(link?.getAttribute("href")).toBe("http://127.0.0.1:18025");
    expect(link?.textContent).toBe("http://127.0.0.1:18025");
    expect(link?.parentElement?.nextSibling?.textContent).toBe("。");
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
      <ChatMarkdown content="**https://example.com/a?b=c&d=1**。" />,
    );

    const link = container.querySelector("strong > a");
    expect(link?.getAttribute("href")).toBe(
      "https://example.com/a?b=c&d=1",
    );
    expect(link?.textContent).toBe("https://example.com/a?b=c&d=1");
    expect(link?.parentElement?.nextSibling?.textContent).toBe("。");
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
    const diagram = "```\n┌──┐\n│你│\n└──┘\n```";
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
