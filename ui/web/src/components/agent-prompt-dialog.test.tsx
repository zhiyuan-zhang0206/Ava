// PromptDialog: textarea-backed modal that collects an initial prompt for a
// lifecycle action. Tested in isolation (no menu) — submit is gated on a
// non-empty trimmed value, submit forwards the trimmed text, Cancel closes.

import {
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { PromptDialog } from "./agent-prompt-dialog";

afterEach(cleanup);

// Radix Dialog calls pointer-capture / scroll APIs happy-dom lacks.
beforeAll(() => {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.scrollIntoView = () => undefined;
});

const baseProps = {
  open: true,
  onOpenChange: () => undefined,
  title: "Fork Agent #5",
  placeholder: "Initial prompt…",
  submitLabel: "Fork",
  onSubmit: () => undefined,
};

describe("PromptDialog", () => {
  it("renders the title and the submit button", () => {
    render(<PromptDialog {...baseProps} />);
    expect(screen.getByText("Fork Agent #5")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Fork" })).toBeTruthy();
  });

  it("submit is disabled while the textarea is empty / whitespace-only", () => {
    const onSubmit = vi.fn();
    render(<PromptDialog {...baseProps} onSubmit={onSubmit} />);
    const submit = screen.getByRole<HTMLButtonElement>("button", { name: "Fork" });
    expect(submit.disabled).toBe(true);
    fireEvent.change(screen.getByPlaceholderText("Initial prompt…"), {
      target: { value: "   " },
    });
    expect(submit.disabled).toBe(true);
    // whitespace-only never submits even if the click lands
    fireEvent.click(submit);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("submit forwards the trimmed text and requests close", () => {
    const onSubmit = vi.fn();
    const onOpenChange = vi.fn();
    render(
      <PromptDialog {...baseProps} onSubmit={onSubmit} onOpenChange={onOpenChange} />,
    );
    fireEvent.change(screen.getByPlaceholderText("Initial prompt…"), {
      target: { value: "  explore auth  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Fork" }));
    expect(onSubmit).toHaveBeenCalledWith("explore auth");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("Cmd/Ctrl+Enter in the textarea submits", () => {
    const onSubmit = vi.fn();
    render(<PromptDialog {...baseProps} onSubmit={onSubmit} />);
    const textarea = screen.getByPlaceholderText("Initial prompt…");
    fireEvent.change(textarea, { target: { value: "go" } });
    fireEvent.keyDown(textarea, { key: "Enter", metaKey: true });
    expect(onSubmit).toHaveBeenCalledWith("go");
  });

  it("Cancel closes without submitting", () => {
    const onSubmit = vi.fn();
    const onOpenChange = vi.fn();
    render(
      <PromptDialog {...baseProps} onSubmit={onSubmit} onOpenChange={onOpenChange} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});
