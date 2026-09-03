import { describe, expect, it } from "vitest";
import { observationText } from "./agent-observation";

describe("independent observation evidence", () => {
  it("does not invent runtime ownership from a fresh machine or lease", () => {
    const now = Date.parse("2026-09-03T00:00:00Z");
    const evidence = {
      machine_probe_at: new Date(now - 10_000).toISOString(),
      machine_probe_valid_until: new Date(now + 110_000).toISOString(),
      runtime_lease_expires_at: new Date(now + 300_000).toISOString(),
      runtime_owner: "unknown" as const,
    };
    expect(observationText(evidence, now)).toContain("fresh (10s ago)");
    expect(observationText(evidence, now)).toContain("runtime owner: unknown");
    expect(observationText(evidence, now + 120_000)).toContain("stale (130s ago)");
  });
  it("unknown evidence is not fresh and expired lease is distinct", () => {
    expect(observationText(undefined)).toContain("Machine probe: unknown");
    expect(observationText({ runtime_owner: "unknown", runtime_lease_expires_at: "2026-01-01T00:00:00Z" }, Date.parse("2026-09-03T00:00:00Z"))).toContain("runtime lease: expired");
  });
});
