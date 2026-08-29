// The frontend server must never be directly reachable off-box.
//
// The fleet UI gate (services/gate) owns the entry port and proxies the
// Next.js app over loopback; the gateway CORS allowlist trusts only the entry
// origin. If the app bound all interfaces (`-H ::`), a browser dialing the
// app port directly would land on an origin outside the CORS allowlist —
// every API call (login included) is then blocked, and Chrome's password
// manager treats the stray origin as a separate site, so credentials saved on
// the entry page never autofill. Binding loopback keeps one origin everywhere.
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

const pkg = JSON.parse(
  readFileSync(join(__dirname, "../../package.json"), "utf8"),
) as { scripts: Record<string, string> };

describe("frontend bind host", () => {
  it("dev binds loopback", () => {
    expect(pkg.scripts.dev).toContain("-H 127.0.0.1");
  });

  it("start binds loopback", () => {
    expect(pkg.scripts.start).toContain("-H 127.0.0.1");
  });

  it("never binds all interfaces", () => {
    expect(pkg.scripts.dev).not.toContain("-H ::");
    expect(pkg.scripts.start).not.toContain("-H ::");
  });
});
