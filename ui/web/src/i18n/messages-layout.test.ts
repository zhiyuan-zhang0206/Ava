// Layout of the message catalogs: one JSON file per namespace under messages/<locale>/,
// listed by that locale's index.ts. A single en.json / zh.json that every UI change
// edits is what this layout replaced, so it must not come back; a namespace file the
// index does not list (or lists under another name) would silently drop its strings.

import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import en from "../../messages/en";
import zh from "../../messages/zh";

const MESSAGES = join(__dirname, "..", "..", "messages");
const CATALOGS = { en, zh } as const;

function namespaceFiles(locale: string): string[] {
  return readdirSync(join(MESSAGES, locale))
    .filter((f) => f.endsWith(".json"))
    .map((f) => f.slice(0, -".json".length))
    .sort();
}

describe("message catalog layout", () => {
  it("has no single-file catalog per locale", () => {
    for (const locale of Object.keys(CATALOGS)) {
      expect(existsSync(join(MESSAGES, `${locale}.json`)), `messages/${locale}.json`).toBe(false);
    }
  });

  it("holds the same namespaces in both locales", () => {
    expect(namespaceFiles("zh")).toEqual(namespaceFiles("en"));
  });

  it.each(Object.entries(CATALOGS))("%s index lists every namespace file under its own name", (locale, catalog) => {
    expect(Object.keys(catalog).sort()).toEqual(namespaceFiles(locale));
    for (const namespace of namespaceFiles(locale)) {
      const onDisk: unknown = JSON.parse(readFileSync(join(MESSAGES, locale, `${namespace}.json`), "utf8"));
      expect((catalog as Record<string, unknown>)[namespace], namespace).toEqual(onDisk);
    }
  });
});
