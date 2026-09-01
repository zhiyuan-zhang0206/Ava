import { describe, expect, it } from "vitest";

import {
  browserFacingRequestUrl,
  buildContentSecurityPolicy,
  gatewayOriginForRequest,
} from "./content-security-policy";

function directive(policy: string, name: string): string {
  return policy.split("; ").find((value) => value.startsWith(`${name} `)) ?? "";
}

describe("gatewayOriginForRequest", () => {
  it("uses the configured API base origin without its path", () => {
    expect(
      gatewayOriginForRequest(new URL("https://console.example.test/control"), {
        apiBase: "https://gateway.example.test:8443/api",
      }),
    ).toBe("https://gateway.example.test:8443");
  });

  it("derives the gateway origin from the requested host and configured port", () => {
    expect(
      gatewayOriginForRequest(new URL("https://console.example.test/control"), {
        gatewayPort: "8443",
      }),
    ).toBe("https://console.example.test:8443");
  });
});

describe("browserFacingRequestUrl", () => {
  it("normalizes an uppercase forwarded protocol", () => {
    expect(
      browserFacingRequestUrl(
        new Request("http://localhost:3109/control", {
          headers: {
            "x-forwarded-host": "console.example.test:3109",
            "x-forwarded-proto": "HTTP",
          },
        }),
      ).origin,
    ).toBe("http://console.example.test:3109");
  });
});

describe("buildContentSecurityPolicy", () => {
  it("uses a nonce instead of unsafe script execution in production", () => {
    const policy = buildContentSecurityPolicy({
      nonce: "request-nonce",
      gatewayOrigin: "https://gateway.example.test:8443",
      isDevelopment: false,
    });

    expect(directive(policy, "script-src")).toBe(
      "script-src 'self' 'nonce-request-nonce' 'strict-dynamic'",
    );
    expect(directive(policy, "style-src")).toBe("style-src 'self' 'nonce-request-nonce'");
    expect(directive(policy, "style-src-attr")).toBe("style-src-attr 'unsafe-inline'");
    expect(directive(policy, "object-src")).toBe("object-src 'none'");
    expect(directive(policy, "base-uri")).toBe("base-uri 'none'");
    expect(directive(policy, "form-action")).toBe("form-action 'self'");
    expect(policy).not.toContain("'unsafe-eval'");
    expect(directive(policy, "script-src")).not.toContain("'unsafe-inline'");
  });

  it("limits connections to the gateway HTTP and WebSocket origins", () => {
    const policy = buildContentSecurityPolicy({
      nonce: "request-nonce",
      gatewayOrigin: "https://gateway.example.test:8443",
      isDevelopment: false,
    });

    expect(directive(policy, "connect-src")).toBe(
      "connect-src 'self' https://gateway.example.test:8443 wss://gateway.example.test:8443",
    );
    expect(directive(policy, "frame-src")).toBe(
      "frame-src 'self' https://gateway.example.test:8443",
    );
  });

  it("keeps the development allowances Next.js needs", () => {
    const policy = buildContentSecurityPolicy({
      nonce: "request-nonce",
      gatewayOrigin: "http://gateway.example.test:8000",
      isDevelopment: true,
    });

    expect(directive(policy, "script-src")).toBe(
      "script-src 'self' 'nonce-request-nonce' 'strict-dynamic' 'unsafe-eval'",
    );
    expect(directive(policy, "style-src")).toBe("style-src 'self' 'unsafe-inline'");
  });
});
