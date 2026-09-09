#!/usr/bin/env node
/**
 * Build-artifact integrity assertion for the minified production bundle.
 *
 * Guards the failure class behind task #2654 (postmortems/0007): a dependency
 * whose dist marks a side-effectful call `@__PURE__` (radix's esbuild
 * keep-names emit, @radix-ui/react-scroll-area >= 1.2.16) is silently emptied
 * by the production minifier. SWC compress trusts the annotation and drops the
 * whole rAF polling IIFE of `addUnlinkedScrollListener` as an unused pure call,
 * then constant-folds the corpse: the bundle still builds and loads, the
 * scrollbar thumb just stops tracking.
 *
 * Tripwire: deny `cancelAnimationFrame(0)` in any emitted chunk. rAF ids start
 * at 1, so canceling frame 0 is only ever the corpse of an eliminated loop
 * (the dead rAF id constant-folded to its initial 0). No legitimate code shape
 * produces it. The check is deliberately shape-independent and name-free: the
 * degraded function's identifiers are mangled differently across dependency
 * versions, but the corpse is what it is in every one of them.
 *
 * What this check does NOT do, on purpose: positive assertions about specific
 * symbols surviving minification. The degraded build was caught by the deny
 * rule alone, and a name-based positive rule (e.g. "the bundle must contain
 * addUnlinkedScrollListener") depends on emit shape that healthy production
 * builds do not have — that variant fails every healthy build, which is worse
 * than no guard (it teaches everyone to bypass the check).
 */

import { readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const CHUNKS_DIR = path.join(".next", "static", "chunks");
const CANCEL_FRAME_ZERO = /cancelAnimationFrame\(\s*0\s*\)/;

/**
 * @param {string} dir
 * @returns {string[]}
 */
export function chunkFiles(dir) {
  const out = [];
  // Recursive: Turbopack may nest route chunks (chunks/app/...) depending on
  // the emit layout — a top-level-only walk would silently stop covering them.
  const stack = [dir];
  while (stack.length > 0) {
    const current = stack.pop();
    if (current === undefined) continue;
    for (const name of readdirSync(current)) {
      const entry = path.join(current, name);
      if (statSync(entry).isDirectory()) stack.push(entry);
      else if (name.endsWith(".js")) out.push(entry);
    }
  }
  return out;
}

/**
 * @param {{ path: string, src: string }[]} chunks
 * @returns {string[]} paths of chunks carrying the corpse signature
 */
export function degradedChunks(chunks) {
  return chunks.filter((c) => CANCEL_FRAME_ZERO.test(c.src)).map((c) => c.path);
}

/**
 * @param {string} chunksDir
 */
export function checkBundleIntegrity(chunksDir = CHUNKS_DIR) {
  const chunks = chunkFiles(chunksDir);
  if (chunks.length === 0) throw new Error(`no chunks found in ${chunksDir}`);
  const degraded = degradedChunks(
    chunks.map((chunk) => ({ path: chunk, src: readFileSync(chunk, "utf8") })),
  );
  if (degraded.length > 0) {
    throw new Error(
      `${degraded[0]}: contains cancelAnimationFrame(0) — a @__PURE__-annotated call ` +
        `was eliminated by the minifier (task #2654 class)`,
    );
  }
}

const entrypoint = process.argv.at(1);
if (entrypoint !== undefined && path.resolve(entrypoint) === fileURLToPath(import.meta.url)) {
  try {
    checkBundleIntegrity();
  } catch (err) {
    console.error(`bundle integrity: ${err instanceof Error ? err.message : String(err)}`);
    process.exit(1);
  }
  process.stdout.write("bundle integrity: ok\n");
}
