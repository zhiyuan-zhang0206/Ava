import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from "./resizable";

afterEach(cleanup);

describe("ResizableHandle", () => {
  it("keeps a broad invisible hit target while highlighting one thin separator", () => {
    const { container } = render(
      <ResizablePanelGroup direction="horizontal">
        <ResizablePanel defaultSize={50} />
        <ResizableHandle />
        <ResizablePanel defaultSize={50} />
      </ResizablePanelGroup>,
    );
    const handle = container.querySelector<HTMLElement>('[data-slot="resizable-handle"]');

    expect(handle).not.toBeNull();
    const classes = handle!.className.split(" ");
    expect(classes).toContain("w-px");
    expect(classes).toContain("z-10");
    expect(classes).toContain("bg-transparent");
    expect(classes).not.toContain("bg-border");
    expect(classes).toContain("before:inset-y-0");
    expect(classes).toContain("before:w-2");
    expect(classes).toContain("after:top-0");
    expect(classes).toContain("after:bottom-0");
    expect(classes).toContain("after:w-px");
    expect(classes).toContain("after:bg-border");
    expect(classes).not.toContain("after:w-1");
  });
});
