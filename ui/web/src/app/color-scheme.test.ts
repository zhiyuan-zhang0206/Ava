// Color-scheme contract (task #2695) — scheme-aware tooling (Dark Reader,
// UA-rendered widgets) reads two signals: a <meta name="color-scheme">
// declaration and the computed `color-scheme` on <html>, which the `.dark`
// CSS rule feeds. Both must exist so the console's dark mode is recognized
// whichever detector branch runs.
//
// History: the console switched theme via next-themes' class toggle only;
// that also pokes an inline `style.color-scheme` on <html>, but an inline
// declaration carries no CSS rule for computed-style detectors that read
// the stylesheet, and the meta was absent entirely. The static meta +
// explicit stylesheet rules make the scheme an intrinsic document signal.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const css = readFileSync(join(__dirname, "globals.css"), "utf8");
const layout = readFileSync(join(__dirname, "layout.tsx"), "utf8");

function blockOf(source: string, selector: string): string {
  const start = source.indexOf(selector + " {");
  expect(start, `${selector} must be declared`).toBeGreaterThan(-1);
  const end = source.indexOf("}", start);
  return source.slice(start, end);
}

describe("color-scheme declarations (task #2695)", () => {
  it(":root declares light and .dark declares dark", () => {
    expect(blockOf(css, ":root")).toContain("color-scheme: light;");
    expect(blockOf(css, ".dark")).toContain("color-scheme: dark;");
  });

  it("layout exports the color-scheme meta covering both schemes", () => {
    expect(layout).toMatch(/import type \{ Metadata, Viewport \} from "next";/);
    expect(layout).toContain('colorScheme: "dark light"');
  });
});
