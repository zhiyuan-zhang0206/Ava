import { describe, expect, it } from "vitest";

import { externalAgentName } from "./inbound-source";

describe("externalAgentName", () => {
  it("names the subject alone", () => {
    expect(externalAgentName("external_agent:claude_code")).toBe("claude_code");
  });

  it("appends the instance", () => {
    expect(externalAgentName("external_agent:codex:run-42")).toBe("codex / run-42");
  });
});
