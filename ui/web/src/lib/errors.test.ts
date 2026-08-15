// errMsg: normalize any thrown value (Error / object with .message /
// string / number / null) into a string. fetch reject and user-code
// throwing arbitrary values should never cause a toast to show
// "undefined".

import { describe, expect, it } from "vitest";

import { errMsg } from "./errors";

describe("errMsg", () => {
  it("Error instance → .message", () => {
    expect(errMsg(new Error("boom"))).toBe("boom");
  });

  it("TypeError subclass also uses .message", () => {
    expect(errMsg(new TypeError("bad type"))).toBe("bad type");
  });

  it("plain object with .message → extracts message", () => {
    expect(errMsg({ message: "from obj", code: 42 })).toBe("from obj");
  });

  it("plain object without .message → JSON.stringify, never the bare '[object Object]' toString", () => {
    expect(errMsg({ code: 42 })).toBe('{"code":42}');
  });

  it("plain string → returned as-is (via String(e))", () => {
    expect(errMsg("plain str")).toBe("plain str");
  });

  it("number → '42'", () => {
    expect(errMsg(42)).toBe("42");
  });

  it("null → 'null'", () => {
    expect(errMsg(null)).toBe("null");
  });

  it("undefined → 'undefined'", () => {
    expect(errMsg(undefined)).toBe("undefined");
  });

  it("object with non-string .message → JSON.stringify, not '[object Object]'", () => {
    expect(errMsg({ message: { nested: "x" } })).toBe('{"message":{"nested":"x"}}');
  });

  it("circular object (unstringifiable) → falls back to String(e), never throws", () => {
    const circular: Record<string, unknown> = { code: 1 };
    circular.self = circular;
    expect(errMsg(circular)).toBe("[object Object]");
  });
});
