import { describe, expect, it } from "vitest";

import type { PluginStat, UiStatContribution } from "./types";
import { buildPluginStatCards, PLUGIN_STAT_STALE_AFTER_MS } from "./plugin-stats";

function declaration(plugin: string, id: string, label: string): UiStatContribution {
  return { plugin, id, label };
}

function stat(over: Partial<PluginStat> = {}): PluginStat {
  return {
    plugin: "codex_usage",
    id: "codex-a",
    value: "6%",
    detail: "94% used - weekly",
    status: "ok",
    updated_at: "2026-09-11T00:00:00+00:00",
    updated_by: "macmini",
    ...over,
  };
}

describe("buildPluginStatCards", () => {
  const now = new Date("2026-09-11T00:10:00+00:00");

  it("joins declarations with values by (plugin, id), in declaration order", () => {
    const cards = buildPluginStatCards(
      [
        declaration("deepseek_balance", "balance", "DeepSeek balance"),
        declaration("codex_usage", "codex-a", "Codex A"),
      ],
      [
        stat(),
        stat({
          plugin: "deepseek_balance",
          id: "balance",
          value: "\u00a52483.37",
          detail: null,
        }),
      ],
      now,
    );
    expect(cards.map((c) => c.key)).toEqual(["deepseek_balance/balance", "codex_usage/codex-a"]);
    expect(cards[0]).toMatchObject({ label: "DeepSeek balance", value: "\u00a52483.37", detail: null });
    expect(cards[1]).toMatchObject({ label: "Codex A", value: "6%", status: "ok" });
  });

  it("a declared card with no value row is the explicit empty state", () => {
    const cards = buildPluginStatCards([declaration("codex_usage", "codex-b", "Codex B")], [], now);
    expect(cards).toEqual([
      {
        key: "codex_usage/codex-b",
        label: "Codex B",
        value: null,
        status: null,
        detail: null,
        updatedAt: null,
        stale: false,
      },
    ]);
  });

  it("a value without a declaration is not rendered", () => {
    const cards = buildPluginStatCards([], [stat({ id: "retired" })], now);
    expect(cards).toEqual([]);
  });

  it("a value older than the stale horizon is marked stale; a fresh one is not", () => {
    const fresh = new Date("2026-09-11T00:00:00+00:00");
    const old = new Date(fresh.getTime() - PLUGIN_STAT_STALE_AFTER_MS - 1000);
    const cards = buildPluginStatCards(
      [declaration("p", "old", "Old"), declaration("p", "fresh", "Fresh")],
      [
        stat({ plugin: "p", id: "old", updated_at: old.toISOString() }),
        stat({ plugin: "p", id: "fresh", updated_at: fresh.toISOString() }),
      ],
      now,
    );
    expect(cards.map((c) => c.stale)).toEqual([true, false]);
  });

  it("exactly at the horizon is not yet stale, and an unparsable date never claims freshness", () => {
    const boundary = new Date(now.getTime() - PLUGIN_STAT_STALE_AFTER_MS);
    const cards = buildPluginStatCards(
      [declaration("p", "boundary", "Boundary"), declaration("p", "broken", "Broken")],
      [
        stat({ plugin: "p", id: "boundary", updated_at: boundary.toISOString() }),
        stat({ plugin: "p", id: "broken", updated_at: "not-a-date" }),
      ],
      now,
    );
    expect(cards[0].stale).toBe(false);
    expect(cards[1].stale).toBe(false);
  });

  it("status and detail ride through for the panel's error/warn rendering", () => {
    const cards = buildPluginStatCards(
      [declaration("codex_usage", "codex-b", "Codex B")],
      [stat({ id: "codex-b", value: "!", detail: "token revoked", status: "error" })],
      now,
    );
    expect(cards[0]).toMatchObject({ status: "error", detail: "token revoked", value: "!" });
  });
});
