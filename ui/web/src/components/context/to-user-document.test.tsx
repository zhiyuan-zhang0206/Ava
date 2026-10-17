import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { ToUserDocument } from "@/lib/contracts/types";

const { linkedQuery } = vi.hoisted(() => ({ linkedQuery: vi.fn() }));
vi.mock("next-intl", () => ({ useTranslations: () => (key: string) => key }));
vi.mock("@/lib/context/use-to-user", () => ({ useToUser: linkedQuery }));
vi.mock("@/components/content/python-code", () => ({ PythonCode: () => null }));
import { preloadChatMarkdown } from "@/components/content/markdown";
import { BAR_DIVIDER_CLASS, BAR_MIN_HEIGHT_CLASS } from "@/lib/layout/layout";
import { ToUserDocumentView } from "./to-user-document";

beforeAll(async () => {
  await preloadChatMarkdown();
});

const id = "a".repeat(64);
const linkId = "b".repeat(64);
function document(content: string, modifiedAt: string | null = null): ToUserDocument {
  return { content, version: "1", modified_at: modifiedAt, legacy_html: false, resources: {
    "/tmp/plot.png": { id, name: "plot.png", markdown: false, version: "asset-v1" },
    "details.md": { id: linkId, name: "details.md", markdown: true, version: "doc-v1" },
    "file:///tmp/file.pdf": { id: "c".repeat(64), name: "file.pdf", markdown: false, version: null },
  } };
}
function idleQuery(data?: ToUserDocument) {
  return { data, isFetching: false, isError: false, refetch: vi.fn() };
}
function show(value: ToUserDocument, { mobile = false, linked }: { mobile?: boolean; linked?: ToUserDocument } = {}) {
  linkedQuery.mockReturnValue(idleQuery(linked));
  return render(<ToUserDocumentView agentId={7} document={value} failed={false} pending={false} refresh={vi.fn()} mobile={mobile} />);
}
function url() {
  return new URL(window.location.href);
}
function chain() {
  return url().searchParams.getAll("reference");
}
function travel(to: string) {
  act(() => {
    window.history.replaceState(null, "", to);
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
}
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.history.replaceState(null, "", "/?agent_id=7");
});

describe("To user Markdown", () => {
  it("renders local images and file URIs through authenticated resource URLs", () => {
    show(document("![plot](/tmp/plot.png)\n\n[report](file:///tmp/file.pdf)"));
    expect(screen.getByRole("img").getAttribute("src")).toContain(`/api/agents/7/to-user/resource?reference=${id}&v=asset-v1`);
    expect(screen.getByRole("link", { name: "report" }).getAttribute("href")).toContain("reference=" + "c".repeat(64));
  });
  it("keeps the shared theme, strips hostile HTML and unregistered filesystem links", () => {
    const { container } = show(document('# Status\n\n<style>body{color:red}</style>\n\n<script>alert(1)</script>\n\n[secret](/private/file)\n\n[site](https://example.com)'));
    expect(screen.getByRole("heading", { name: "Status" })).toBeTruthy();
    expect(container.querySelector(".chat-md.font-sans")).toBeTruthy();
    expect(container.querySelector("iframe,style,script")).toBeNull();
    expect(screen.queryByRole("link", { name: "secret" })).toBeNull();
    expect(screen.getByText("secret")).toBeTruthy();
    expect(screen.getByRole("link", { name: "site" }).getAttribute("target")).toBe("_blank");
  });
  it("renders the safe raw-HTML subset, including registered local images", () => {
    const { container } = show(document('<details><summary>More</summary>body</details>\n\n<img src="/tmp/plot.png" alt="raw plot">'));
    expect(container.querySelector("details > summary")?.textContent).toBe("More");
    expect(container.querySelector("details")?.textContent).toContain("body");
    expect(screen.getByRole("img", { name: "raw plot" }).getAttribute("src")).toContain(`/api/agents/7/to-user/resource?reference=${id}&v=asset-v1`);
  });
  it("keeps modifier clicks as ordinary resource downloads", () => {
    show(document("[Details](details.md)"));
    const link = screen.getByRole("link", { name: "Details" });
    link.addEventListener("click", (event) => event.preventDefault());
    fireEvent.click(link, { ctrlKey: true });
    expect(linkedQuery).toHaveBeenLastCalledWith(null, []);
  });
  it("draws the panel title rule on the shared column title bar", () => {
    show(document("body"));
    const tokens = screen.getByTestId("document-bar").className.split(/\s+/);
    expect(tokens).toEqual(expect.arrayContaining(["relative", BAR_MIN_HEIGHT_CLASS]));
    expect(tokens).toEqual(expect.arrayContaining(BAR_DIVIDER_CLASS.split(" ")));
    expect(tokens).not.toContain("border-b");
  });
  it("shows migration guidance when only a legacy HTML document remains", () => {
    show({ content: null, version: null, modified_at: null, legacy_html: true });
    expect(screen.getByText("legacyHtml")).toBeTruthy();
  });
});

describe("To user navigation history", () => {
  const root = () => document("[Details](details.md)");
  const nested = () => document("# Nested document");

  it("puts the opened document in the URL and follows Back and Forward to it", () => {
    show(root(), { linked: nested() });
    fireEvent.click(screen.getByRole("link", { name: "Details" }));
    expect(chain()).toEqual([linkId]);
    expect(screen.getByRole("heading", { name: "Nested document" })).toBeTruthy();

    travel("/?agent_id=7");
    expect(chain()).toEqual([]);
    expect(screen.getByRole("link", { name: "Details" })).toBeTruthy();

    travel(`/?agent_id=7&reference=${linkId}`);
    expect(chain()).toEqual([linkId]);
    expect(screen.getByRole("heading", { name: "Nested document" })).toBeTruthy();
  });

  it("restores a shared link's chain but not a chain left by another agent", () => {
    window.history.replaceState(null, "", `/?agent_id=7&reference=${linkId}`);
    show(root(), { linked: nested() });
    expect(screen.getByRole("heading", { name: "Nested document" })).toBeTruthy();

    window.history.replaceState(null, "", `/?agent_id=8&reference=${linkId}`);
    travel("/?agent_id=8&reference=" + linkId);
    expect(screen.queryByRole("heading", { name: "Nested document" })).toBeNull();
  });

  it("walks browser history from the panel's back control, falling back to the parent", () => {
    const back = vi.spyOn(window.history, "back").mockImplementation(() => undefined);
    show(root(), { linked: nested() });
    fireEvent.click(screen.getByRole("link", { name: "Details" }));
    fireEvent.click(screen.getByRole("button", { name: "back" }));
    expect(back).toHaveBeenCalledTimes(1);

    back.mockClear();
    window.history.replaceState(null, "", `/?agent_id=7&reference=${linkId}`);
    cleanup();
    show(root(), { linked: nested() });
    fireEvent.click(screen.getByRole("button", { name: "back" }));
    expect(back).not.toHaveBeenCalled();
    expect(chain()).toEqual([]);
    expect(screen.getByRole("link", { name: "Details" })).toBeTruthy();
  });
});

describe("To user document bar", () => {
  it("groups the document time with the refresh control at the right edge", () => {
    show(document("# Status", "2026-10-17T06:00:00Z"));
    const status = screen.getByTestId("document-status");
    expect(status.parentElement?.lastElementChild).toBe(status);
    expect(status.className).toContain("ml-auto");
    expect(status.className).toContain("shrink-0");
    expect(within(status).getByText("updated")).toBeTruthy();
    expect(within(status).getByRole("button", { name: "refresh" })).toBeTruthy();
    expect(screen.getByText("sharedContext").contains(status)).toBe(false);
  });

  it("does not repeat the panel title in the mobile reading view", () => {
    show(document("# Status"), { mobile: true });
    expect(screen.queryByText("sharedContext")).toBeNull();
    expect(screen.getByTestId("document-status")).toBeTruthy();
  });
});
