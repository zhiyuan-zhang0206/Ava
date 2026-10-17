import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppSettingsEntry, readAppBridge } from "./app-settings";

interface Internals {
  __TAURI_INTERNALS__?: { invoke: (command: string) => Promise<unknown> };
}

afterEach(() => {
  delete (window as unknown as Internals).__TAURI_INTERNALS__;
});

describe("readAppBridge", () => {
  it("stays null outside the Meiva app", () => {
    expect(readAppBridge()).toBeNull();
  });

  it("asks the app to open its settings page", async () => {
    const invoke = vi.fn().mockResolvedValue(undefined);
    (window as unknown as Internals).__TAURI_INTERNALS__ = { invoke };
    const bridge = readAppBridge();
    expect(bridge).not.toBeNull();
    await bridge?.openSettings();
    expect(invoke).toHaveBeenCalledWith("app_open_settings");
  });
});

describe("AppSettingsEntry", () => {
  it("invokes the settings command on click", async () => {
    const invoke = vi.fn().mockResolvedValue(undefined);
    (window as unknown as Internals).__TAURI_INTERNALS__ = { invoke };
    render(<AppSettingsEntry bridge={readAppBridge()!} />);
    fireEvent.click(screen.getByRole("button"));
    await waitFor(() => expect(invoke).toHaveBeenCalledWith("app_open_settings"));
  });

  it("surfaces a rejected invocation", async () => {
    const invoke = vi.fn().mockRejectedValue(new Error("denied"));
    (window as unknown as Internals).__TAURI_INTERNALS__ = { invoke };
    render(<AppSettingsEntry bridge={readAppBridge()!} />);
    fireEvent.click(screen.getByRole("button"));
    expect(await screen.findByRole("status")).toBeTruthy();
  });
});
