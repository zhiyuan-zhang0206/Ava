// UploadButton component test — locks render tri-state (normal /
// uploading / error) + interaction (click triggers file input ->
// onChange calls onUpload with the selected files) + edge cases
// (multi-select, multi-file progress counter, agentId=null disables it).
//
// happy-dom + RTL — vitest globals=false explicit cleanup, mirrors composer.test.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { UploadButton } from "./upload-button";

afterEach(cleanup);

// Create a real File-like object for testing
function makeFile(name = "test.png", type = "image/png", size = 1024): File {
  // happy-dom supports File constructor
  return new File([new ArrayBuffer(size)], name, { type });
}

const baseProps = {
  agentId: 1,
  onUpload: vi.fn(() => Promise.resolve()),
  uploading: false,
  progress: 0,
  count: 0,
  error: null,
};

describe("UploadButton rendering", () => {
  it("default state — renders Paperclip button + hidden file input", () => {
    render(<UploadButton {...baseProps} />);

    // Paperclip button visible
    const btn = screen.getByRole("button", { name: "Upload file" });
    expect(btn).toBeDefined();
    expect((btn as HTMLButtonElement).disabled).toBe(false);

    // hidden file input present (type=file)
    const input = document.querySelector('input[type="file"]');
    expect(input).not.toBeNull();
    expect((input as HTMLInputElement).disabled).toBe(false);
  });

  it("file input has the multiple attribute (multi-select enabled)", () => {
    render(<UploadButton {...baseProps} />);
    const input = document.querySelector<HTMLInputElement>('input[type="file"]')!;
    expect(input.multiple).toBe(true);
  });

  it("agentId=null → button and input both disabled", () => {
    render(<UploadButton {...baseProps} agentId={null} />);

    const btn = screen.getByRole("button", { name: "Upload file" });
    expect((btn as HTMLButtonElement).disabled).toBe(true);

    const input = document.querySelector('input[type="file"]');
    expect((input as HTMLInputElement).disabled).toBe(true);
  });

  it("uploading=true → shows spinner and progress percentage", () => {
    render(<UploadButton {...baseProps} uploading={true} progress={42} />);

    // Progress text
    expect(screen.getByText("42%")).toBeDefined();

    // Button and input should not exist
    expect(screen.queryByRole("button")).toBeNull();
    expect(document.querySelector('input[type="file"]')).toBeNull();
  });

  it("uploading a multi-file batch → shows file count + percentage (N files · pct)", () => {
    render(<UploadButton {...baseProps} uploading={true} count={3} progress={42} />);
    expect(screen.getByText("3 files · 42%")).toBeDefined();
  });

  it("uploading a single file → shows percentage only (no count prefix)", () => {
    render(<UploadButton {...baseProps} uploading={true} count={1} progress={42} />);
    expect(screen.getByText("42%")).toBeDefined();
  });

  it("error non-null → shows AlertCircle; button + input remain (clickable to retry)", () => {
    render(<UploadButton {...baseProps} error="Network error" />);

    // Button still there, red + AlertCircle icon, title says click to retry
    const btn = screen.getByRole("button");
    expect(btn.className).toContain("text-destructive");
    expect(btn.getAttribute("aria-label")).toContain("Network error");
    expect(btn.getAttribute("aria-label")).toContain("click to retry");

    // input also still present — clicking button → triggers input click → onChange → onUpload
    // → uploadHandler calls setUploadError(null) to clear the error naturally
    expect(document.querySelector('input[type="file"]')).not.toBeNull();
  });

  it("error state click button → triggers file picker (retry path)", () => {
    render(<UploadButton {...baseProps} error="Network error" />);

    const input = document.querySelector<HTMLInputElement>('input[type="file"]')!;
    const clickSpy = vi.spyOn(input, "click");

    fireEvent.click(screen.getByRole("button"));
    expect(clickSpy).toHaveBeenCalled();
  });

  it("progress=0 still shows 0%", () => {
    render(<UploadButton {...baseProps} uploading={true} progress={0} />);
    expect(screen.getByText("0%")).toBeDefined();
  });
});

describe("UploadButton interaction", () => {
  it("click button → triggers click on hidden file input", () => {
    const onUpload = vi.fn(() => Promise.resolve());
    render(<UploadButton {...baseProps} onUpload={onUpload} />);

    const input = document.querySelector<HTMLInputElement>('input[type="file"]')!;
    const clickSpy = vi.spyOn(input, "click");

    fireEvent.click(screen.getByRole("button", { name: "Upload file" }));
    expect(clickSpy).toHaveBeenCalled();
  });

  it("after selecting one file → calls onUpload([file]) and clears input.value", () => {
    const onUpload = vi.fn(() => Promise.resolve());
    render(<UploadButton {...baseProps} onUpload={onUpload} />);

    const file = makeFile("photo.png", "image/png", 2048);
    const input = document.querySelector<HTMLInputElement>('input[type="file"]')!;

    // Trigger onChange via fireEvent.change
    fireEvent.change(input, { target: { files: [file] } });

    expect(onUpload).toHaveBeenCalledTimes(1);
    expect(onUpload).toHaveBeenCalledWith([file]);

    // input.value should be cleared (allows re-uploading the same file)
    expect(input.value).toBe("");
  });

  it("selecting multiple files → calls onUpload with all of them, in order", () => {
    const onUpload = vi.fn(() => Promise.resolve());
    render(<UploadButton {...baseProps} onUpload={onUpload} />);

    const f1 = makeFile("a.png", "image/png", 1024);
    const f2 = makeFile("b.pdf", "application/pdf", 2048);
    const f3 = makeFile("c.txt", "text/plain", 64);
    const input = document.querySelector<HTMLInputElement>('input[type="file"]')!;

    fireEvent.change(input, { target: { files: [f1, f2, f3] } });

    expect(onUpload).toHaveBeenCalledTimes(1);
    expect(onUpload).toHaveBeenCalledWith([f1, f2, f3]);
    expect(input.value).toBe("");
  });

  it("no file selected (files empty) → does not call onUpload", () => {
    const onUpload = vi.fn(() => Promise.resolve());
    render(<UploadButton {...baseProps} onUpload={onUpload} />);

    const input = document.querySelector<HTMLInputElement>('input[type="file"]')!;
    fireEvent.change(input, { target: { files: [] } });

    expect(onUpload).not.toHaveBeenCalled();
  });
});

describe("UploadButton disabled states", () => {
  it("when disabled, button and input both have disabled attribute", () => {
    render(<UploadButton {...baseProps} agentId={null} />);

    // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-assertion -- narrow getByRole result to HTMLButtonElement to access .disabled
    expect((screen.getByRole("button") as HTMLButtonElement).disabled).toBe(true);
    expect(document.querySelector<HTMLInputElement>('input[type="file"]')!.disabled).toBe(true);
  });
});
