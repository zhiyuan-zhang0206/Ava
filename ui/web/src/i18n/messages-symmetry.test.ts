// en/zh message catalog symmetry — next-intl's default fallback is the
// English catalog, so a zh catalog key missing from en (or vice versa) is
// INVISIBLE in the UI: the missing key renders the English string and no
// error fires. messages/en is the canonical catalog (the AppConfig Messages type
// anchors to it, giving useTranslations() compile-time key checking), but
// nothing type-checks the zh catalog's shape against it — this test pins the two
// catalogs to identical key sets, in both directions.

import { createTranslator } from "next-intl";
import { describe, expect, it } from "vitest";

import en from "../../messages/en";
import zh from "../../messages/zh";

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
    expect(missingInZh, `keys in en missing from zh: ${missingInZh.join(", ")}`).toEqual([]);
    expect(missingInEn, `keys in zh missing from en: ${missingInEn.join(", ")}`).toEqual([]);
  });

  it("zh covers the context breakdown surface (title, labels)", () => {
    const zhCard = createTranslator({ locale: "zh", messages: zh, namespace: "contextBreakdown" });
    expect(zhCard("title")).toBe("\u4e0a\u4e0b\u6587\u6784\u6210");
    expect(zhCard("categories.system_prompt")).toBe("\u7cfb\u7edf\u63d0\u793a\u8bcd");
    expect(zhCard("categories.tool_response")).toBe("\u5de5\u5177\u8f93\u51fa");
  });
});
