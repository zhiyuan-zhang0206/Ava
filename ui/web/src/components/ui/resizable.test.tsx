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
    expect(handle!.className).toContain("w-px");
    expect(handle!.className).toContain("z-10");
    expect(handle!.className).toContain("before:w-2");
    expect(handle!.className).toContain("after:w-px");
    expect(handle!.className).not.toContain("after:w-1");
  });
});
