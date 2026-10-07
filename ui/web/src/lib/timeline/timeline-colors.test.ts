// timeline-colors.ts — the configurable timeline palette's pure core.
//
// Pins three contracts the Display panel and the timeline both rely on:
// 1) every family x slot combination resolves to real class strings (a missing
//    CLASSES shape would surface as `undefined` inside a className),
// 2) illegal or absent settings fall back to the slot default, and
// 3) resolution is memoized on the input signature — unchanged settings return
//    the SAME object reference, which is what keeps the timeline's per-item
//    config cache (row.tsx) from recomputing on every render.

import { describe, expect, it } from "vitest";

import {
  COLOR_FAMILIES,
  COLOR_FAMILY_LABELS,
  COLOR_ITEM_LABELS,
  COLOR_SLOT_IDS,
  FAMILY_SWATCH,
  TIMELINE_COLOR_ITEMS,
  isColorFamily,
  resolveTimelineColors,
} from "./timeline-colors";

describe("palette tables", () => {
  it("every family has a swatch and a display label", () => {
    for (const family of COLOR_FAMILIES) {
      expect(FAMILY_SWATCH[family]).toMatch(/^bg-[a-z]+-500$/);
      expect(COLOR_FAMILY_LABELS[family]).toBeTruthy();
    }
  });

  it("every slot has a settings key, a row label, and a valid default", () => {
    for (const id of COLOR_SLOT_IDS) {
      const spec = TIMELINE_COLOR_ITEMS[id];
      expect(spec.key).toBe(`display.color.${id}`);
      expect(COLOR_ITEM_LABELS[id]).toBeTruthy();
      expect(isColorFamily(spec.default)).toBe(true);
    }
  });

  it("every family resolves every slot's shapes to non-empty classes", () => {
    for (const family of COLOR_FAMILIES) {
      const seeded: Record<string, unknown> = {};
      for (const id of COLOR_SLOT_IDS) seeded[TIMELINE_COLOR_ITEMS[id].key] = family;
      const colors = resolveTimelineColors(seeded);
      for (const id of COLOR_SLOT_IDS) {
        const spec = TIMELINE_COLOR_ITEMS[id];
        const resolved = colors[id];
        expect(resolved.border).toContain(family);
        if (spec.bg) expect(resolved.bg).toContain(family);
        else expect(resolved.bg).toBeNull();
        if (spec.text) expect(resolved.text).toContain(family);
        else expect(resolved.text).toBeNull();
      }
    }
  });
});

describe("resolveTimelineColors", () => {
  it("empty settings → the user-approved defaults", () => {
    const colors = resolveTimelineColors({});
    expect(colors.agent_chat.border).toBe("border-emerald-500/50");
    expect(colors.agent_code.border).toBe("border-cyan-500/70");
    expect(colors.code_output.border).toBe("border-amber-500/50");
    expect(colors.reasoning.border).toBe("border-blue-400/40");
    expect(colors.reasoning.bg).toBe("bg-blue-50/60 dark:bg-blue-900/20");
    expect(colors.note.border).toBe("border-teal-500/40");
    expect(colors.note.text).toBe("text-teal-700 dark:text-teal-300");
    expect(colors.lifecycle_terminate.border).toBe("border-rose-400/60");
  });

  it("a recorded family override repaints the slot", () => {
    const colors = resolveTimelineColors({ "display.color.note": "fuchsia" });
    expect(colors.note.border).toBe("border-fuchsia-500/40");
    expect(colors.note.bg).toBe("bg-fuchsia-50/40 dark:bg-fuchsia-950/15");
    expect(colors.note.text).toBe("text-fuchsia-700 dark:text-fuchsia-300");
    // Untouched slots keep their defaults.
    expect(colors.memory.border).toBe("border-violet-400/60");
  });

  it("unknown or non-string values fall back to the default", () => {
    const colors = resolveTimelineColors({
      "display.color.note": "chartreuse",
      "display.color.memory": 7,
      "display.color.attach": null,
    });
    expect(colors.note.border).toBe("border-teal-500/40");
    expect(colors.memory.border).toBe("border-violet-400/60");
    expect(colors.attach.border).toBe("border-sky-400/50");
  });

  it("memoizes on the input signature — same settings, same reference", () => {
    const first = resolveTimelineColors({ "display.color.note": "pink" });
    const second = resolveTimelineColors({ "display.color.note": "pink" });
    expect(second).toBe(first);
    // A changed value must re-resolve.
    const changed = resolveTimelineColors({ "display.color.note": "cyan" });
    expect(changed).not.toBe(first);
    expect(changed.note.border).toBe("border-cyan-500/40");
  });
});
