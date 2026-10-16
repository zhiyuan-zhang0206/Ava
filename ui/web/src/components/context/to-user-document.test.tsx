import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { ToUserDocument } from "@/lib/contracts/types";

const { linkedQuery } = vi.hoisted(() => ({ linkedQuery: vi.fn() }));
vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
vi.mock("@/lib/context/use-to-user", () => ({ useToUser: linkedQuery }));
vi.mock("@/components/content/python-code", () => ({ PythonCode: () => null }));
import { preloadChatMarkdown } from "@/components/content/markdown";
import { ToUserDocumentView } from "./to-user-document";

beforeAll(async () => {
  await preloadChatMarkdown();
});

const id = "a".repeat(64);
function document(content: string): ToUserDocument {
  return { content, version: "1", modified_at: null, legacy_html: false, resources: {
    "/tmp/plot.png": { id, name: "plot.png", markdown: false, version: "asset-v1" },
    "details.md": { id: "b".repeat(64), name: "details.md", markdown: true, version: "doc-v1" },
    "file:///tmp/file.pdf": { id: "c".repeat(64), name: "file.pdf", markdown: false, version: null },
  } };
}
function show(value: ToUserDocument) {
  linkedQuery.mockReturnValue({ data: undefined, isFetching: false, isError: false, refetch: vi.fn() });
  return render(<ToUserDocumentView agentId={7} document={value} failed={false} pending={false} refresh={vi.fn()} />);
}
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("To user Markdown", () => {
  it("renders local images and file URIs through authenticated resource URLs", () => {
    show(document("![plot](/tmp/plot.png)\n\n[report](file:///tmp/file.pdf)"));
    expect(screen.getByRole("img").getAttribute("src")).toContain(`/api/agents/7/to-user/resource?reference=${id}&v=asset-v1`);
    expect(screen.getByRole("link", { name: "report" }).getAttribute("href")).toContain("reference=" + "c".repeat(64));
  });
  it("uses the shared theme and blocks raw HTML and unregistered filesystem links", () => {
    const { container } = show(document('# Status\n\n<style>body{color:red}</style>\n\n<script>alert(1)</script>\n\n[secret](/private/file)\n\n[site](https://example.com)'));
    expect(screen.getByRole("heading", { name: "Status" })).toBeTruthy();
    expect(container.querySelector(".chat-md.font-sans")).toBeTruthy();
    expect(container.querySelector("iframe,style,script")).toBeNull();
    expect(screen.getByRole("link", { name: "secret" }).getAttribute("href")).toBe("about:blank");
    expect(screen.getByRole("link", { name: "site" }).getAttribute("target")).toBe("_blank");
  });
  it("opens linked Markdown in the panel and supports returning to its parent", () => {
    show(document("[Details](details.md)"));
    linkedQuery.mockReturnValue({ data: document("# Nested document"), isFetching: false, isError: false, refetch: vi.fn() });
    fireEvent.click(screen.getByRole("link", { name: "Details" }));
    expect(linkedQuery).toHaveBeenLastCalledWith(7, ["b".repeat(64)]);
    expect(screen.getByRole("heading", { name: "Nested document" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "back" }));
    expect(screen.getByRole("link", { name: "Details" })).toBeTruthy();
  });
  it("keeps modifier clicks as ordinary resource downloads", () => {
    show(document("[Details](details.md)"));
    const link = screen.getByRole("link", { name: "Details" });
    link.addEventListener("click", (event) => event.preventDefault());
    fireEvent.click(link, { ctrlKey: true });
    expect(linkedQuery).toHaveBeenLastCalledWith(null, []);
  });
  it("shows migration guidance when only a legacy HTML document remains", () => {
    show({ content: null, version: null, modified_at: null, legacy_html: true });
    expect(screen.getByText("legacyHtml")).toBeTruthy();
  });
});
