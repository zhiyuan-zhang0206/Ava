import { existsSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// The console deliberately ships no favicon or icon (task #3286). Next only
// injects an icon <link> when one of its icon conventions exists, and the
// state has slipped back once already — a branded favicon was restored
// inside an unrelated polish PR (#1858). Lock the empty state at the file
// level; see node_modules/next/dist/docs (app-icons file conventions) for
// the convention list this mirrors.
const ICON_CONVENTION = /^(favicon\.ico|icon\.(ico|jpe?g|png|svg|tsx?|jsx?)|apple-icon\.(jpe?g|png|tsx?|jsx?))$/i;

function iconConventionFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return iconConventionFiles(path);
    return ICON_CONVENTION.test(entry.name) ? [path] : [];
  });
}

describe("no favicon", () => {
  it("ships no icon-convention file under src/app", () => {
    expect(iconConventionFiles("src/app")).toEqual([]);
  });

  it("ships no public favicon fallback", () => {
    expect(existsSync("public/favicon.ico")).toBe(false);
  });
});
