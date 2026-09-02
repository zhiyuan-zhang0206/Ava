// en/zh message catalog symmetry — next-intl's default fallback is the
// English catalog, so a zh.json key missing from en.json (or vice versa) is
// INVISIBLE in the UI: the missing key renders the English string and no
// error fires. en.json is the canonical catalog (the AppConfig Messages type
// anchors to it, giving useTranslations() compile-time key checking), but
// nothing type-checks zh.json's shape against it — this test pins the two
// catalogs to identical key sets, in both directions.

import { createTranslator } from "next-intl";
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

  it("formats unmatched run counts with locale-correct singular copy", () => {
    const enTimeline = createTranslator({ locale: "en", messages: en, namespace: "runTimeline" });
    const zhTimeline = createTranslator({ locale: "zh", messages: zh, namespace: "runTimeline" });

    expect(enTimeline("unmatchedWarning", { count: 1 })).toBe(
      "Tracing data is unavailable for 1 turn (missing span IDs used time-window fallback or remained unmatched).",
    );
    expect(enTimeline("unmatchedWarning", { count: 2 })).toBe(
      "Tracing data is unavailable for 2 turns (missing span IDs used time-window fallback or remained unmatched).",
    );
    expect(zhTimeline("unmatchedWarning", { count: 1 })).toBe(
      "1 \u4e2a turn \u7f3a\u5c11 tracing \u5173\u8054\u6570\u636e\uff08span_id \u7f3a\u5931\u540e\u5df2\u6309\u65f6\u95f4\u7a97\u56de\u9000\uff0c\u6216\u4ecd\u672a\u5339\u914d\uff09\u3002",
    );
    expect(zhTimeline("unmatchedWarning", { count: 2 })).toBe(
      "2 \u4e2a turn \u7f3a\u5c11 tracing \u5173\u8054\u6570\u636e\uff08span_id \u7f3a\u5931\u540e\u5df2\u6309\u65f6\u95f4\u7a97\u56de\u9000\uff0c\u6216\u4ecd\u672a\u5339\u914d\uff09\u3002",
    );
  });
});
