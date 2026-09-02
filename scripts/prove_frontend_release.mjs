/** CI-only real standalone HTTP proof with the source checkout temporarily absent. */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";
import { frontendInputs, prepareFrontend, verifyFrontend } from "./prepare_frontend_release.mjs";

const checkout = fs.realpathSync(process.env.GITHUB_WORKSPACE);
const temporary = fs.realpathSync(process.env.RUNNER_TEMP);
assert.equal(process.env.GITHUB_ACTIONS, "true", "this proof is CI-only");
const frontend = path.join(checkout, "ui", "web");
const target = path.join(temporary, "frontend-image");
const node = fs.realpathSync(process.execPath);
// Negative prepare gates use a tiny input, not a fake HTTP acceptance server.
const fixture = path.join(temporary, "frontend-negative-input");
fs.mkdirSync(path.join(fixture, ".next", "standalone"), { recursive: true });
fs.mkdirSync(path.join(fixture, ".next", "static"));
fs.mkdirSync(path.join(fixture, "public"));
fs.writeFileSync(path.join(fixture, ".next", "standalone", "server.js"), "// fixture\n");
fs.writeFileSync(path.join(fixture, ".next", "static", "chunk.js"), "// asset\n");
fs.writeFileSync(path.join(fixture, "public", "asset.txt"), "fixture asset\n");
const fixtureInputs = frontendInputs(fixture, node);
fs.writeFileSync(path.join(fixture, ".next", "static", "chunk.js"), "// changed\n");
const rejected = path.join(temporary, "frontend-rejected");
assert.throws(() => prepareFrontend(fixture, node, rejected, fixtureInputs), /inputs changed/);
assert(!fs.existsSync(rejected), "changed input wrote a generation");
fs.writeFileSync(path.join(fixture, ".next", "standalone", ".env.production"), "SECRET=fixture-only\n");
assert.throws(() => frontendInputs(fixture, node), /dotenv/);
const expected = frontendInputs(frontend, node);
const receipt = prepareFrontend(frontend, node, target, expected);
const hash = createHash("sha256").update(fs.readFileSync(path.join(target, "frontend-manifest.json"))).digest("hex");
verifyFrontend(target, hash);
const retired = `${checkout}-frontend-proof-retired`;
assert(!fs.existsSync(retired));
let child;
let log = "";
try {
  process.chdir(temporary);
  fs.renameSync(checkout, retired);
  child = spawn(path.join(target, "node"), [path.join(target, "server", "server.js")], {
    cwd: target,
    env: { PATH: "/usr/bin:/bin", HOME: temporary, NODE_ENV: "production", PORT: "43871", HOSTNAME: "127.0.0.1", NEXT_TELEMETRY_DISABLED: "1" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout.on("data", (chunk) => { log = (log + chunk.toString()).slice(-12000); });
  child.stderr.on("data", (chunk) => { log = (log + chunk.toString()).slice(-12000); });
  const url = "http://127.0.0.1:43871";
  let html;
  for (let attempt = 0; attempt < 60; attempt++) {
    if (child.exitCode !== null) throw new Error(`standalone exited: ${log}`);
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(1000) });
      if (response.ok) { html = await response.text(); break; }
    } catch { /* Startup connection refusal is bounded, never acceptance. */ }
    await delay(500);
  }
  assert(html, `standalone never served HTTP: ${log}`);
  const resources = [...html.matchAll(/(?:src|href)="([^" ]*\/_next\/static\/[^" ]+)"/g)].map((item) => item[1]);
  assert(resources.length > 0, "rendered page has no traced static resources");
  for (const resource of [...new Set(resources)].slice(0, 6)) {
    const response = await fetch(new URL(resource, url), { signal: AbortSignal.timeout(3000) });
    assert(response.ok, `missing standalone resource ${resource}`);
    assert((await response.arrayBuffer()).byteLength > 0);
  }
  const publicFile = Object.keys(expected.public).find((name) => !name.startsWith("."));
  if (publicFile) {
    assert((await fetch(`${url}/${publicFile}`, { signal: AbortSignal.timeout(3000) })).ok);
  }

  // A failed later prepare cannot replace the existing serving image.
  assert.throws(() => prepareFrontend(path.join(retired, "ui", "web"), node, target, expected));
  assert((await fetch(url, { signal: AbortSignal.timeout(3000) })).ok);
  verifyFrontend(target, hash);
  fs.writeFileSync(path.join(temporary, "frontend-proof.json"), JSON.stringify({
    sourceAbsentHttp: true, staticResources: resources.length, publicAsset: publicFile ?? null,
    changedInputRejectedBeforeWrite: true, dotenvRejected: true,
    servingImageSurvivesFailedPrepare: true, manifestHash: hash,
    nodeVersion: receipt.nodeVersion, platform: receipt.platform, architecture: receipt.arch,
  }) + "\n", { flag: "wx", mode: 0o600 });
} finally {
  if (child && child.exitCode === null) {
    child.kill("SIGTERM");
    await Promise.race([new Promise((resolve) => child.once("exit", resolve)), delay(5000)]);
    assert(child.exitCode !== null || child.signalCode !== null, "CI standalone did not stop");
  }
  if (fs.existsSync(retired) && !fs.existsSync(checkout)) fs.renameSync(retired, checkout);
}
