
import { Window } from "happy-dom";
import { readFileSync } from "node:fs";
import { describe, it, expect } from "vitest";
import path from "node:path";

const APP_UI = path.resolve(process.cwd(), "../app/app-ui");

function loadPage(language: string) {
  let html = readFileSync(path.join(APP_UI, "index.html"), "utf-8");
  const en = readFileSync(path.join(APP_UI, "locales/en.js"), "utf-8");
  const zh = readFileSync(path.join(APP_UI, "locales/zh.js"), "utf-8");
  html = html.replace('<script src="locales/en.js"></script>', `<script>${en}</script>`);
  html = html.replace('<script src="locales/zh.js"></script>', `<script>${zh}</script>`);
  const window = new Window({ url: "http://tauri.localhost/index.html#setup", settings: { enableJavaScriptEvaluation: true } });
  const tauriWindow = window as Window & {
    __TAURI_INTERNALS__?: {
      invoke: (cmd: string, args?: unknown) => Promise<unknown>;
    };
  };
  tauriWindow.__TAURI_INTERNALS__ = {
    invoke: () =>
      Promise.resolve({
        entryUrl: "",
        platform: "android" as const,
        gatewayUrl: "",
        backgroundService: false,
        notifications: false,
      }),
  };
  Object.defineProperty(window, "navigator", { value: { language }, configurable: true });
  window.document.write(html);
  window.document.close();
  return window;
}

describe("app-ui locale + autofill", () => {
  it("zh system locale renders the setup screen in Chinese", () => {
    const w = loadPage("zh-CN");
    const doc = w.document;
    expect(doc.querySelector("[data-i18n='connectTitle']")!.textContent).toBe("\u8fde\u63a5\u5230 Ava");
    expect(doc.documentElement.lang).toBe("zh-CN");
    expect(doc.getElementById("server")!.getAttribute("placeholder")).toBe("\u4e3b\u673a\u6216 http://\u4e3b\u673a:\u7aef\u53e3");
    expect(doc.getElementById("setup-submit")!.textContent).toBe("\u8fde\u63a5");
    expect(doc.getElementById("secret-visibility")!.textContent).toBe("\u663e\u793a");
    expect(doc.querySelector("#connecting-steps li")!.textContent).toBe("\u63a2\u6d4b\u63a7\u5236\u53f0");
  });

  it("en system locale keeps the English defaults", () => {
    const w = loadPage("en-US");
    const doc = w.document;
    expect(doc.querySelector("[data-i18n='connectTitle']")!.textContent).toBe("Connect to Ava");
    expect(doc.documentElement.lang).toBe("en");
    expect(doc.getElementById("server")!.getAttribute("placeholder")).toBe("host or http://host:port");
  });

  it("form carries standard autofill semantics", () => {
    const w = loadPage("en-US");
    const doc = w.document;
    expect(doc.getElementById("server")!.getAttribute("autocomplete")).toBe("username");
    expect(doc.getElementById("cluster-secret")!.getAttribute("autocomplete")).toBe("current-password");
  });

  it("no private-overlay IP literal remains anywhere in the page", () => {
    const html = readFileSync(path.join(APP_UI, "index.html"), "utf-8");
    expect(html).not.toMatch(/100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b/);
  });
});
