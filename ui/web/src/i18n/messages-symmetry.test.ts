// en/zh message catalog symmetry — next-intl's default fallback is the
// English catalog, so a zh.json key missing from en.json (or vice versa) is
// INVISIBLE in the UI: the missing key renders the English string and no
// error fires. en.json is the canonical catalog (the AppConfig Messages type
// anchors to it, giving useTranslations() compile-time key checking), but
// nothing type-checks zh.json's shape against it — this test pins the two
// catalogs to identical key sets, in both directions.

import { describe, expect, it } from "vitest";

import en from "../../messages/en.json";
import zh from "../../messages/zh.json";

function flattenKeys(obj: object, prefix = ""): Set<string> {
  const keys = new Set<string>();
  for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
    const path = prefix ? `${prefix}.${k}` : k;
    if (v !== null && typeof v === "object") {
      for (const sub of flattenKeys(v, path)) {
        keys.add(sub);
      }
    } else {
      keys.add(path);
    }
  }
  return keys;
}

describe("i18n message catalogs", () => {
  it("en and zh expose the same key set (no silent English fallback)", () => {
    const enKeys = flattenKeys(en);
    const zhKeys = flattenKeys(zh);
    const missingInZh = [...enKeys].filter((k) => !zhKeys.has(k)).sort();
    const missingInEn = [...zhKeys].filter((k) => !enKeys.has(k)).sort();
    expect(missingInZh, `keys in en.json missing from zh.json: ${missingInZh.join(", ")}`).toEqual([]);
    expect(missingInEn, `keys in zh.json missing from en.json: ${missingInEn.join(", ")}`).toEqual([]);
  });
});
