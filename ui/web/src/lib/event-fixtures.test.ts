// Wire-format equivalence test (TS side) — shares fixtures with
// `tests/test_event_fixtures.py`; both sides parsing successfully ==
// Python ↔ TS alignment.
//
// Every SystemEvent role gets one case here: read fixture JSON, narrow
// to the right subclass, assert every required field is present.
// Adding a new role on the frontend without syncing: assertSystemEvent
// falls into assertNever, TS fails at compile time and runtime.
// Adding a new required field on the backend without syncing the
// frontend: TS struct narrow can't catch it (TS only checks at compile
// time), but the per-role required-fields list is hard-coded here —
// update this file when the backend adds fields (alongside fixture
// regeneration).

import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import type { SystemEvent } from "./types";

const FIXTURES_DIR = join(__dirname, "../../../../tests/fixtures/events");

function loadFixture(role: string): unknown {
  return JSON.parse(readFileSync(join(FIXTURES_DIR, `${role}.json`), "utf-8"));
}

function listFixtureRoles(): string[] {
  return readdirSync(FIXTURES_DIR)
    .filter((f) => f.endsWith(".json"))
    .map((f) => f.slice(0, -".json".length))
    .sort();
}

// Required fields per role — when the backend Pydantic schema
// changes (adds a required field), update this together with the
// fixtures. Adding a role on the frontend without listing required
// fields: assertSystemEventShape falls through to default →
// assert_never goes red at compile time (exhaustive switch).
const REQUIRED_FIELDS: Record<SystemEvent["role"], readonly string[]> = {
  chat_start: ["agent_id", "role", "item_id"],
  chat_delta: ["agent_id", "role", "item_id", "content"],
  code_start: ["agent_id", "role", "item_id"],
  code_delta: ["agent_id", "role", "item_id", "content"],
  reasoning_start: ["agent_id", "role", "item_id"],
  reasoning_delta: ["agent_id", "role", "item_id", "content"],
  exec_start: ["agent_id", "role"],
  exec_output_chunk: ["agent_id", "role", "item_id", "content"],
  exec_output: ["agent_id", "role", "item_id", "content"],
  compact_request: ["agent_id", "role", "content"],
  compact_done: ["agent_id", "role"],
  error: ["agent_id", "role", "content"],
  cancelled: ["agent_id", "role"],
  inbound_arrived: ["agent_id", "role", "inbound_id", "kind", "source", "content"],
  inbound_committed: ["agent_id", "role", "inbound_id"],
  label_updated: ["agent_id", "role", "label"],
  page_opened: ["agent_id", "role", "page_id", "name", "port", "title", "url"],
  page_closed: ["agent_id", "role", "name"],
  token_usage: ["agent_id", "role", "input_tokens", "output_tokens", "reasoning_tokens"],
  llm_done: ["agent_id", "role"],
  timeline_snapshot: ["agent_id", "role", "items", "msg_count"],
  agent_spawned: ["agent_id", "role", "snapshot"],
  agent_updated: ["agent_id", "role", "snapshot"],
  notice_posted: ["agent_id", "role", "notice_id", "priority", "title", "task_id"],
  notice_resolved: ["agent_id", "role", "notice_id"],
  task_created: ["agent_id", "role", "task_id"],
  task_updated: ["agent_id", "role", "task_id"],
  cluster_update_started: ["agent_id", "role", "kind", "origin"],
};

const ALL_ROLES: readonly SystemEvent["role"][] = Object.keys(
  REQUIRED_FIELDS,
) as SystemEvent["role"][];

function isRecord(x: unknown): x is Record<string, unknown> {
  return typeof x === "object" && x !== null;
}

function assertNever(_x: never): never {
  // eslint-disable-next-line @typescript-eslint/restrict-template-expressions -- exhaustiveness check pattern
  throw new Error(`unhandled role: ${_x}`);
}

function assertSystemEventShape(obj: unknown): asserts obj is SystemEvent {
  if (!isRecord(obj)) throw new Error("event must be object");
  const role = obj.role;
  if (typeof role !== "string") throw new Error("event.role missing or not string");

  // Narrow via switch + assert_never — when the backend adds a role
  // without syncing TS, REQUIRED_FIELDS lacks this case →
  // SystemEvent["role"] doesn't include it → switch falls through to
  // default with a string (not never) → assertNever type-errors or
  // throws at runtime.
  const r = role as SystemEvent["role"];
  switch (r) {
    case "chat_start":
    case "chat_delta":
    case "code_start":
    case "code_delta":
    case "reasoning_start":
    case "reasoning_delta":
    case "exec_start":
    case "exec_output_chunk":
    case "exec_output":
    case "compact_request":
    case "compact_done":
    case "error":
    case "cancelled":
    case "inbound_arrived":
    case "inbound_committed":
    case "label_updated":
    case "page_opened":
    case "page_closed":
    case "token_usage":
    case "llm_done":
    case "timeline_snapshot":
    case "agent_spawned":
    case "agent_updated":
    case "notice_posted":
    case "notice_resolved":
    case "task_created":
    case "task_updated":
    case "cluster_update_started":
      break;
    default:
      assertNever(r);
  }

  const required = REQUIRED_FIELDS[r];
  for (const field of required) {
    if (!(field in obj)) {
      throw new Error(`event role=${r} missing required field ${field}`);
    }
  }
}

describe("SystemEvent wire fixtures", () => {
  // 1. Fixtures cover every role the frontend knows about — when the
  //    backend adds a role without syncing the frontend, this goes
  //    red (fixture file exists but REQUIRED_FIELDS has no entry).
  it("fixtures mirror the SystemEvent role union 1:1", () => {
    const fixtureRoles = new Set<string>(listFixtureRoles());
    // Compare with a widened string set — SystemEvent["role"] is a
    // narrow union; strict TS rejects .has() with a string arg. Both
    // sides use Set<string> for set diff.
    const knownRoles = new Set<string>(ALL_ROLES);
    const missing = [...knownRoles].filter((r) => !fixtureRoles.has(r));
    const extra = [...fixtureRoles].filter((r) => !knownRoles.has(r));
    expect(missing, "SystemEvent has a role with no fixture").toEqual([]);
    expect(
      extra,
      "fixture folder has a role not in the SystemEvent union (frontend not adapted OR fixture stale)",
    ).toEqual([]);
  });

  // 2. Every fixture can narrow and pass the required-fields
  //    assertion — when the backend adds a required field without
  //    syncing: REQUIRED_FIELDS hasn't been updated for that field,
  //    so even though the fixture has it the assertion list doesn't —
  //    this test would still pass (frontend has no knowledge of the
  //    field). But the Python wire test catches it (strict Pydantic
  //    validation rejects the old fixture). Running both sides = drift
  //    is caught.
  it.each(ALL_ROLES)("%s fixture passes shape assertion", (role) => {
    const obj = loadFixture(role);
    expect(() => assertSystemEventShape(obj)).not.toThrow();
    expect((obj as { role: string }).role).toBe(role);
  });

  // 3. No extra fixtures in the folder (= stale role file forgotten)
  it("fixture directory size equals ALL_ROLES", () => {
    expect(listFixtureRoles().length).toBe(ALL_ROLES.length);
  });
});
