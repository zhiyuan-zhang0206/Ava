import { describe, expect, it } from "vitest";

import nextConfig from "./next.config";

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
