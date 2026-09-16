import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { runInNewContext } from "node:vm";

import { describe, expect, it, vi } from "vitest";

const template = readFileSync(resolve(process.cwd(), "../../services/gate/static/login.html"), "utf8");

describe("gate login through the HTTPS browser entry", () => {
  it.each([
    ["https://console.example", "/api/auth/login"],
    ["http://192.0.2.2:20017", "http://192.0.2.2:20016/api/auth/login"],
    ["https://other.example", "http://192.0.2.2:20016/api/auth/login"],
  ])("submits credentials to the correct origin from %s", async (origin, expected) => {
    const html = template
      .replaceAll("__GATEWAY_BASE__", "http://192.0.2.2:20016")
      .replaceAll("/*__BROWSER_ORIGIN__*/", JSON.stringify("https://console.example"));
    const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
    const loginScript = scripts.at(-1)![1];
    let submit: ((event: { preventDefault: () => void }) => Promise<void>) | undefined;
    const elements: Record<string, object> = {
      form: { addEventListener: (_name: string, callback: typeof submit) => { submit = callback; } },
      password: { value: "test-password", addEventListener: vi.fn() },
      username: { value: "admin" }, submit: {}, error: {},
    };
    const fetch = vi.fn().mockResolvedValue({ ok: true });
    const location = { origin, href: "" };
    runInNewContext(loginScript, {
      navigator: { language: "en" }, window: { location }, location, fetch,
      document: { getElementById: (id: string) => elements[id] },
    });
    expect(submit).toBeDefined();
    await submit!({ preventDefault: vi.fn() });
    expect(fetch).toHaveBeenCalledWith(expected, expect.objectContaining({
      method: "POST", credentials: "include",
    }));
    expect(location.href).toBe("/");
  });
});
