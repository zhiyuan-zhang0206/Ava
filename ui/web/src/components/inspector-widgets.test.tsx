// InspectWidgetSection — the console renderer for plugin inspector widgets
// (task #2909; taskList reshaped in #3216). A widget is data: a known kind
// renders the kernel-resolved payload under the section chrome, unknown kinds
// are skipped, and an empty payload renders nothing.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/link", () => ({
  default: ({ children, href, ...rest }: { children: React.ReactNode; href: string }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

import type { InspectWidget } from "@/lib/types";

import { InspectWidgetSection } from "./inspector-widgets";

afterEach(cleanup);

function widget(over: Partial<InspectWidget> = {}): InspectWidget {
  return {
    plugin: "ava_fleet",
    id: "today-tasks",
    kind: "taskList",
    order: 150,
    title: null,
    tasks: [
      { id: 42, title: "Ship the inspector fix", priority: "P0" },
      { id: 43, title: "Reply to QA", priority: "P3" },
    ],
    ...over,
  };
}

describe("InspectWidgetSection", () => {
  it("renders one fleet-task link per task row under the console default title", () => {
    render(<InspectWidgetSection widget={widget()} />);
    // The console's own localized title stands in when the plugin set none.
    expect(screen.getByText("Tasks")).toBeTruthy();
    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(2);
    expect(links[0].getAttribute("href")).toBe("/fleet?task=42");
    expect(links[1].getAttribute("href")).toBe("/fleet?task=43");
    expect(links[0].textContent).toContain("Ship the inspector fix");
    expect(links[0].textContent).toContain("#42");
  });

  it("leads each row with #id, then its priority badge, then the title (tasks #3563/#3866)", () => {
    render(<InspectWidgetSection widget={widget()} />);
    const link = screen.getAllByRole("link")[0];
    const spans = [...link.querySelectorAll("span")].map((s) => s.textContent);
    // The id column renders first (left edge, #3563); the priority rung sits
    // immediately right of it (#3866 — not the row's right edge); the title
    // takes the remaining width.
    expect(spans).toEqual(["#42", "P0", "Ship the inspector fix"]);
  });

  it("shows each row's priority badge (P0..P3, the board's own colors)", () => {
    render(<InspectWidgetSection widget={widget()} />);
    const rows = screen.getAllByRole("link");
    // The badge is the span right of the #id column (index 1).
    const badges = rows.map((row) => [...row.querySelectorAll("span")][1]);
    expect(badges.map((b) => b.textContent)).toEqual(["P0", "P3"]);
    // Same PRIORITY_BG mapping as the task board / graph (P0 destructive).
    expect(badges[0]?.className).toContain("bg-destructive");
    expect(badges[1]?.className).toContain("bg-slate-500");
  });

  it("uses a plugin-declared title when given", () => {
    render(<InspectWidgetSection widget={widget({ title: "Agent tasks" })} />);
    expect(screen.getByText("Agent tasks")).toBeTruthy();
    expect(screen.queryByText("Tasks")).toBeNull();
  });

  it("renders every task row — no display cap (user ruling 2026-09-17)", () => {
    const tasks = Array.from({ length: 11 }, (_, i) => ({
      id: 100 + i,
      title: `task ${i}`,
      priority: "P2" as const,
    }));
    render(<InspectWidgetSection widget={widget({ tasks })} />);
    expect(screen.getAllByRole("link")).toHaveLength(11);
  });

  it("renders nothing for an unknown kind", () => {
    const { container } = render(
      <InspectWidgetSection widget={widget({ kind: "kv" as InspectWidget["kind"] })} />,
    );
    expect(container.querySelectorAll("a")).toHaveLength(0);
  });

  it("renders nothing for an empty task list", () => {
    const { container } = render(<InspectWidgetSection widget={widget({ tasks: [] })} />);
    expect(container.querySelectorAll("a")).toHaveLength(0);
  });
});
