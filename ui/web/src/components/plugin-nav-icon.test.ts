// The nav vocabularies have two halves each, and each pair must be one set.
//
// `shared/plugin_ui_contributions.py:NAV_ICONS` is what a manifest may declare;
// `PLUGIN_NAV_ICONS` is what the console can draw. A name in the validator but
// not the map renders a fallback icon nobody asked for; a name in the map but
// not the validator is a promise no manifest can use. Neither shows up at
// runtime, so it is asserted here — against the Python file itself, not a
// transcription of it.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { PLUGIN_NAV_ICONS } from "@/components/plugin-nav-icon";
import { NAV_LOCATIONS } from "@/lib/plugin-nav";

function validatorTuple(name: string): string[] {
  const source = readFileSync(
    resolve(__dirname, "../../../../shared/plugin_ui_contributions.py"),
    "utf-8",
  );
  // Non-greedy to the first `)`, so a one-line tuple (NAV_LOCATIONS) and a
  // multi-line one (NAV_ICONS) both read correctly.
  const block = new RegExp(`^${name} = \\(([\\s\\S]*?)\\)`, "m").exec(source);
  expect(block, `${name} tuple not found in plugin_ui_contributions.py`).not.toBeNull();
  return [...block![1].matchAll(/"([a-z0-9-]+)"/g)].map((m) => m[1]);
}

describe("plugin nav vocabularies", () => {
  it("draws exactly the icon names the manifest validator accepts", () => {
    const declared = validatorTuple("NAV_ICONS");
    expect(declared.length).toBeGreaterThan(0);
    expect(Object.keys(PLUGIN_NAV_ICONS).sort()).toEqual([...declared].sort());
  });

  it("renders exactly the locations the manifest validator accepts", () => {
    const declared = validatorTuple("NAV_LOCATIONS");
    expect(declared.length).toBeGreaterThan(0);
    expect([...NAV_LOCATIONS].sort()).toEqual([...declared].sort());
  });

  it("maps every name to a renderable component", () => {
    for (const [name, Icon] of Object.entries(PLUGIN_NAV_ICONS)) {
      expect(Icon, name).toBeTruthy();
      expect(typeof Icon === "function" || typeof Icon === "object", name).toBe(true);
    }
  });
});
