import { describe, expect, it, vi } from "vitest";
import type { ConfigFieldView, ConfigWriteResult } from "@/lib/contracts/types";
import { writeConfigOwners } from "./_owner_writes";

const fields: Pick<ConfigFieldView, "name" | "owner">[] = [
  { name: "core_value", owner: null },
  { name: "fleet_value", owner: "ava_fleet" },
  { name: "fleet_other", owner: "ava_fleet" },
];
const ok = (target: string): ConfigWriteResult => ({
  applied: true,
  results: {},
  restart_required: [target],
});

describe("owned config writes", () => {
  it("sends one flat body per owner and combines successful restart targets", async () => {
    const write = vi
      .fn()
      .mockResolvedValueOnce(ok("agent"))
      .mockResolvedValueOnce(ok("all"));
    const result = await writeConfigOwners(
      { core_value: 1, fleet_value: false, fleet_other: null },
      fields,
      write,
    );
    expect(write.mock.calls).toEqual([
      [{ core_value: 1 }],
      [{ fleet_value: false, fleet_other: null }],
    ]);
    expect(result.applied).toBe(true);
    expect(result.restart_required).toEqual(["agent", "all"]);
  });
  it("shows partial failure without discarding a committed owner's restart", async () => {
    const write = vi
      .fn()
      .mockResolvedValueOnce(ok("agent"))
      .mockRejectedValueOnce(new Error("stale image"));
    const result = await writeConfigOwners(
      { core_value: 1, fleet_value: false },
      fields,
      write,
    );
    expect(result.applied).toBe(false);
    expect(result.results.fleet_value).toEqual({
      ok: false,
      reason: "stale image",
    });
    expect(result.restart_required).toEqual(["agent"]);
  });
  it("preserves field-level rejection and continues other independent owners", async () => {
    const write = vi
      .fn()
      .mockResolvedValueOnce({
        applied: false,
        results: { core_value: { ok: false, reason: "invalid" } },
        restart_required: [],
      })
      .mockResolvedValueOnce(ok("all"));
    const result = await writeConfigOwners(
      { core_value: 1, fleet_value: false },
      fields,
      write,
    );
    expect(result.results.core_value.ok).toBe(false);
    expect(result.applied).toBe(false);
    expect(result.restart_required).toEqual(["all"]);
  });
  it("rejects unknown metadata before sending any owner", async () => {
    const write = vi.fn();
    await expect(
      writeConfigOwners({ core_value: 1, unknown: 2 }, fields, write),
    ).rejects.toThrow("unknown config field");
    expect(write).not.toHaveBeenCalled();
  });
});
