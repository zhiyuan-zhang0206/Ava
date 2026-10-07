import { describe, expect, it } from "vitest";

import { gatewayOriginForRequest } from "./gateway-origin";

const options = { browserOrigin: "https://console.example", gatewayPort: "20016" };

describe("staged HTTPS browser entry", () => {
  it("keeps API, SSE and CSP on the browser-facing HTTP/2 origin", () => {
    expect(gatewayOriginForRequest(new URL("https://console.example/?agent_id=42"), options))
      .toBe("https://console.example");
  });

  it.each([
    ["http://192.0.2.2:20017/", "http://192.0.2.2:20016"],
    ["http://console.example:20017/", "http://console.example:20016"],
    ["https://other.example:8443/", "https://other.example:20016"],
    ["https://console.example:8443/", "https://console.example:20016"],
    ["http://[::1]:20017/", "http://[::1]:20016"],
  ])("preserves the direct entry %s", (input, expected) => {
    expect(gatewayOriginForRequest(new URL(input), options)).toBe(expected);
  });

  it("preserves an explicit development API override", () => {
    expect(gatewayOriginForRequest(new URL("https://console.example"), {
      ...options, apiBase: "http://localhost:8001",
    })).toBe("http://localhost:8001");
  });
});
