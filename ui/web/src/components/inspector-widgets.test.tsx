// InspectWidgetSection — the console renderer for plugin inspector widgets
// (task #2909). A widget is data: known kinds render links from resolved
// targets, unknown kinds/targets are skipped, empty widgets render nothing.

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
    id: "jump-buttons",
    kind: "jumpButtons",
    order: 50,
    title: null,
    buttons: [
      { target: "notice", label: null, icon: null, notice_id: 7, task_id: null },
      { target: "task", label: null, icon: null, notice_id: null, task_id: 42 },
    ],
    ...over,
  };
}

describe("InspectWidgetSection", () => {
  it("renders one link per resolved target, pointing at the fleet route", () => {
    render(<InspectWidgetSection widget={widget()} />);
    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(2);
    expect(links[0].getAttribute("href")).toBe("/fleet?notice=7");
    expect(links[1].getAttribute("href")).toBe("/fleet?task=42");
  });

  it("renders nothing for an unknown kind", () => {
    const { container } = render(
      <InspectWidgetSection widget={widget({ kind: "kv" as InspectWidget["kind"] })} />,
    );
    expect(container.querySelectorAll("a")).toHaveLength(0);
  });

  it("skips an unknown target and an unresolved one", () => {
    render(
      <InspectWidgetSection
        widget={widget({
          buttons: [
            { target: "agent" as never, label: null, icon: null, notice_id: null, task_id: null },
            { target: "notice", label: null, icon: null, notice_id: null, task_id: null },
            { target: "task", label: null, icon: null, notice_id: null, task_id: 42 },
          ],
        })}
      />,
    );
    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(1);
    expect(links[0].getAttribute("href")).toBe("/fleet?task=42");
  });

  it("renders nothing when no button resolves", () => {
    const { container } = render(
      <InspectWidgetSection
        widget={widget({
          buttons: [{ target: "task", label: null, icon: null, notice_id: null, task_id: null }],
        })}
      />,
    );
    expect(container.querySelectorAll("a")).toHaveLength(0);
  });

  it("uses a declared label, and shows the task id alongside it", () => {
    render(
      <InspectWidgetSection
        widget={widget({
          buttons: [
            { target: "task", label: "Open task", icon: null, notice_id: null, task_id: 42 },
          ],
        })}
      />,
    );
    const link = screen.getByRole("link");
    expect(link.textContent).toContain("Open task");
    expect(link.textContent).toContain("#42");
  });

  it("renders a titled widget with the section header", () => {
    render(<InspectWidgetSection widget={widget({ title: "Quick jumps" })} />);
    expect(screen.getByText("Quick jumps")).toBeTruthy();
  });
});
