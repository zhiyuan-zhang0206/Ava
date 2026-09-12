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
    order: 50,
    title: null,
    tasks: [
      { id: 42, title: "Ship the inspector fix" },
      { id: 43, title: "Reply to QA" },
    ],
    ...over,
  };
}

describe("InspectWidgetSection", () => {
  it("renders one fleet-task link per task row under the console default title", () => {
    render(<InspectWidgetSection widget={widget()} />);
    // The console's own localized title stands in when the plugin set none.
    expect(screen.getByText("Today's tasks")).toBeTruthy();
    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(2);
    expect(links[0].getAttribute("href")).toBe("/fleet?task=42");
    expect(links[1].getAttribute("href")).toBe("/fleet?task=43");
    expect(links[0].textContent).toContain("Ship the inspector fix");
    expect(links[0].textContent).toContain("#42");
  });

  it("uses a plugin-declared title when given", () => {
    render(<InspectWidgetSection widget={widget({ title: "Agent tasks" })} />);
    expect(screen.getByText("Agent tasks")).toBeTruthy();
    expect(screen.queryByText("Today's tasks")).toBeNull();
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
