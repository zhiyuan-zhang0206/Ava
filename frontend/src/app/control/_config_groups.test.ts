// Display-grouping table invariants for the Config section (_config_groups.ts).
//
// The page buckets fields into 17 semantic display groups by env var, and
// hides a small editorial list (HIDDEN_ENV_VARS) entirely. The two tables must
// stay disjoint — a var in both would be doubly bookkept ("shown here, hidden
// there"), and the whole point of the hidden list is that the var has NO
// display-group home. This pins that invariant so a future edit to either
// table fails loudly instead of silently re-introducing the dual-track.

import { describe, expect, it } from "vitest";

import { GROUP_ENV_VARS, HIDDEN_ENV_VARS } from "./_config_groups";

describe("config display grouping tables", () => {
  it("keeps hidden env vars out of every display group", () => {
    const mapped = new Set(Object.values(GROUP_ENV_VARS).flat());
    const overlap = [...HIDDEN_ENV_VARS].filter((v) => mapped.has(v));
    expect(overlap).toEqual([]);
  });
});
