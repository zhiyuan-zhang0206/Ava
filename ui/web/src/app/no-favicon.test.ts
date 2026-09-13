import { existsSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

// The console deliberately ships no favicon or icon (task #3286). Next only
// injects an icon <link> when one of its icon conventions exists, and the
// state has slipped back once already — a branded favicon was restored
// inside an unrelated polish PR (#1858). Lock the empty state at the file
// level by mirroring Next's strict metadata-file scanner
// (node_modules/next/dist/lib/metadata/is-metadata-route.js): `favicon.ico`
// is exact; `icon`/`apple-icon` accept a single-digit variant (`\d?` —
// `icon12` is not a convention) over the image or page extensions; no hash
// suffix survives the strict match.
const ICON_CONVENTION =
  /^(favicon\.ico|icon\d?\.(ico|jpe?g|png|svg|tsx?|jsx?)|apple-icon\d?\.(jpe?g|png|tsx?|jsx?))$/i;

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

  it("matches every icon convention Next accepts", () => {
    for (const name of ["favicon.ico", "icon.png", "icon1.png", "apple-icon1.tsx"]) {
      expect(ICON_CONVENTION.test(name), name).toBe(true);
    }
    // Two digits, favicon variants, and hash suffixes are not conventions.
    for (const name of ["icon12.png", "favicon1.ico", "icon-abc123.png"]) {
      expect(ICON_CONVENTION.test(name), name).toBe(false);
    }
  });
});
