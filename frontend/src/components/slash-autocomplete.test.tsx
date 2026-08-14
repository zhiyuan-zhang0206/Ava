// Unit tests for the pure slash-command helpers. Component behavior (dropdown
// open/filter/keyboard/select) is covered through the Composer integration in
// composer.test.tsx, where the two are actually wired together.

import { describe, expect, it } from "vitest";

import { fuzzyScore, parseSlash } from "./slash-autocomplete";

describe("parseSlash", () => {
  // "|" marks the caret and is stripped before parsing — every case here is
  // about which token the caret is in, so spell it out inline rather than
  // counting offsets.
  const at = (marked: string) => parseSlash(marked.replace("|", ""), marked.indexOf("|"));

  it("returns null when the caret isn't on a slash token", () => {
    expect(at("hello|")).toBeNull();
    expect(at("|")).toBeNull();
    expect(parseSlash("", 0)).toBeNull();
  });

  it("'/' alone lists everything (empty query)", () => {
    expect(at("/|")).toMatchObject({ start: 0, end: 1, query: "" });
  });

  it("captures the command name while it's being typed", () => {
    expect(at("/rec|")).toMatchObject({ start: 0, end: 4, query: "rec" });
  });

  it("returns null once the caret is past the name, in the instruction", () => {
    expect(at("/recap |")).toBeNull();
    expect(at("/recap just the P|Rs")).toBeNull();
    // Even with a later command on the line: this caret is in command 1's
    // instruction, not on a name.
    expect(at("/recap just| the PRs /compact")).toBeNull();
  });

  // ── First-token rule (user ruling #836): only the message's LEADING
  // command triggers — a slash mid-message never opens the dropdown. ──

  it("serves only the first command — a later command on the line does not trigger", () => {
    expect(at("/compact ping /rec|")).toBeNull();
  });

  it("leading whitespace before the command is fine (still the first token)", () => {
    expect(at(" /rec|")).toMatchObject({ start: 1, end: 5, query: "rec" });
    expect(at("  /rec| hello world")).toMatchObject({ start: 2, end: 6, query: "rec" });
  });

  it("a slash wedged mid-text does not trigger", () => {
    expect(at("look at /rec|")).toBeNull();
    expect(at("please /rec| now")).toBeNull();
    expect(at("hello /world|")).toBeNull();
  });

  it("newline starts a NEW message — a command on a later line does not trigger", () => {
    expect(at("line one\n/rec|")).toBeNull();
  });

  // ── Whole-token matching ──

  it("matches on the whole token, so a caret parked mid-name still completes it", () => {
    expect(at("/re|cap")).toMatchObject({ start: 0, end: 6, query: "recap" });
  });

  it("text glued to the right of the caret belongs to the query (no hidden replace)", () => {
    // A name typed directly in front of existing text with no separator: the
    // query is the whole glued run, which matches nothing upstream, so the
    // dropdown stays shut instead of swallowing "hello" on select.
    expect(at("/rec|hello")).toMatchObject({ start: 0, end: 9, query: "rechello" });
  });

  it("a path-shaped token parses as a query that simply won't match", () => {
    expect(at("/etc/hosts|")).toMatchObject({ query: "etc/hosts" });
  });

  it("clamps a caret outside the value", () => {
    expect(parseSlash("/rec", 99)).toMatchObject({ start: 0, end: 4, query: "rec" });
    expect(parseSlash("/rec", -5)).toMatchObject({ start: 0, end: 4, query: "rec" });
  });
});

describe("fuzzyScore", () => {
  it("returns 100 for exact match", () => {
    expect(fuzzyScore("recap", "recap")).toBe(100);
  });

  it("returns 80 for prefix match", () => {
    expect(fuzzyScore("rec", "recap")).toBe(80);
    expect(fuzzyScore("reca", "recap")).toBe(80);
  });

  it("returns positive score for subsequence match", () => {
    const score = fuzzyScore("rp", "recap");
    expect(score).toBeGreaterThan(0);
    expect(score).toBeLessThan(80);
  });

  it("returns -1 for no match", () => {
    expect(fuzzyScore("xyz", "recap")).toBe(-1);
  });

  it("handles case-insensitive matching", () => {
    expect(fuzzyScore("RECAP", "recap")).toBe(100);
    expect(fuzzyScore("Rec", "recap")).toBe(80);
  });

  it("consecutive matches score higher than scattered matches", () => {
    const consecutiveScore = fuzzyScore("re", "recap");
    const scatteredScore = fuzzyScore("rp", "recap");
    expect(consecutiveScore).toBeGreaterThan(scatteredScore);
  });

  it("start-of-word match scores higher", () => {
    const startScore = fuzzyScore("r", "recap");
    const midScore = fuzzyScore("e", "recap");
    expect(startScore).toBe(80); // prefix
    expect(midScore).toBeLessThan(80);
  });
});
