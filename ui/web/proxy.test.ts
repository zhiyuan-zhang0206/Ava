import { NextRequest } from "next/server";
import { describe, expect, it } from "vitest";

import { proxy } from "./src/proxy";

function nonceFrom(policy: string): string {
  return /'nonce-([^']+)'/.exec(policy)?.[1] ?? "";
}

describe("proxy", () => {
  it("issues a fresh nonce CSP for each page response", () => {
    const first = proxy(new NextRequest("https://console.example.test/control"));
    const second = proxy(new NextRequest("https://console.example.test/control"));
    const firstPolicy = first.headers.get("Content-Security-Policy") ?? "";
    const secondPolicy = second.headers.get("Content-Security-Policy") ?? "";

    expect(firstPolicy).toContain("connect-src 'self' https://console.example.test:8000 wss://console.example.test:8000");
    expect(nonceFrom(firstPolicy)).not.toBe("");
    expect(nonceFrom(secondPolicy)).not.toBe(nonceFrom(firstPolicy));
  });

  it("derives the fallback gateway origin from the forwarded browser origin", () => {
    const response = proxy(
      new NextRequest("http://localhost:3109/control", {
        headers: {
          "x-forwarded-host": "127.0.0.1:3109",
          "x-forwarded-proto": "https",
        },
      }),
    );

    expect(response.headers.get("Content-Security-Policy")).toContain(
      "connect-src 'self' https://127.0.0.1:8000 wss://127.0.0.1:8000",
    );
  });
});
