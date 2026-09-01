// Legacy /settings → /control redirect — the one behavior in next.config.ts
// worth pinning: an old bookmark/link must still resolve after the rename.

import { describe, expect, it } from "vitest";

import nextConfig from "./next.config";

describe("redirects", () => {
  it("sends /settings to /control", async () => {
    const redirects = await nextConfig.redirects?.();
    expect(redirects).toContainEqual({
      source: "/settings",
      destination: "/control",
      permanent: false,
    });
  });

  it("sends /settings/:path* to /control/:path* (sub-routes + preserves the path)", async () => {
    const redirects = await nextConfig.redirects?.();
    expect(redirects).toContainEqual({
      source: "/settings/:path*",
      destination: "/control/:path*",
      permanent: false,
    });
  });
});

describe("security headers", () => {
  it("leaves CSP to the request-nonce proxy while retaining the static headers", async () => {
    const configuredHeaders = await nextConfig.headers?.();
    const headerValues = new Map(configuredHeaders?.[0]?.headers.map(({ key, value }) => [key, value]));

    expect(headerValues.get("X-Frame-Options")).toBe("DENY");
    expect(headerValues.get("X-Content-Type-Options")).toBe("nosniff");
    expect(headerValues.get("Referrer-Policy")).toBe("strict-origin-when-cross-origin");
    expect(headerValues.has("Content-Security-Policy")).toBe(false);
  });
});
