/** Prepare an inactive standalone image; never install, build, activate or start. */
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

const sha = (bytes) => createHash("sha256").update(bytes).digest("hex");
const nodeVersion = `v${fs.readFileSync(new URL("./runtime-node-version", import.meta.url), "utf8").trim()}`;

export function recordFrontendBuildConfig(frontend, gatewayPort, apiBase = "") {
  if (!/^[0-9]+$/.test(gatewayPort) || Number(gatewayPort) < 1 || Number(gatewayPort) > 65535 || apiBase !== "") {
    throw new Error("standalone build currently supports only a public gateway port and default API base");
  }
  fs.writeFileSync(path.join(frontend, ".next", "frontend-build-config.json"),
    JSON.stringify({ gatewayPort: Number(gatewayPort), apiBase }) + "\n", { flag: "wx", mode: 0o600 });
}

function inventory(root, prefix = "") {
  const files = {};
  for (const name of fs.readdirSync(path.join(root, prefix)).sort()) {
    const relative = prefix ? `${prefix}/${name}` : name;
    const absolute = path.join(root, relative);
    const info = fs.lstatSync(absolute);
    if (name === ".env" || name.startsWith(".env.")) {
      throw new Error("frontend image must not contain dotenv files");
    }
    if (info.isDirectory()) Object.assign(files, inventory(root, relative));
    else if (info.isFile() && info.nlink === 1) files[relative] = sha(fs.readFileSync(absolute));
    else throw new Error(`frontend input is not a private regular tree: ${relative}`);
  }
  return files;
}

function sameInventory(actual, expected) {
  return JSON.stringify(actual) === JSON.stringify(expected);
}

export function frontendInputs(frontend, node) {
  if (process.version !== nodeVersion) throw new Error("Node differs from the release input pin");
  if (process.platform === "win32") throw new Error("retained frontend Node proof currently supports POSIX only");
  if (fs.realpathSync(node) !== fs.realpathSync(process.execPath)) {
    throw new Error("prepare must execute under the same Node binary it retains");
  }
  const standalone = path.join(frontend, ".next", "standalone");
  if (!fs.existsSync(path.join(standalone, "server.js"))) {
    throw new Error("standalone server missing; build with AVA_FRONTEND_RELEASE=1 before preparation");
  }
  const config = JSON.parse(fs.readFileSync(path.join(frontend, ".next", "frontend-build-config.json")));
  if (Object.keys(config).sort().join(",") !== "apiBase,gatewayPort"
      || !Number.isInteger(config.gatewayPort) || config.gatewayPort < 1 || config.gatewayPort > 65535
      || config.apiBase !== "") {
    throw new Error("unsupported public frontend build configuration");
  }
  return {
    standalone: inventory(standalone),
    static: inventory(path.join(frontend, ".next", "static")),
    public: fs.existsSync(path.join(frontend, "public")) ? inventory(path.join(frontend, "public")) : {},
    node: sha(fs.readFileSync(node)),
    publicBuildConfig: config,
  };
}

export function prepareFrontend(frontend, node, target, expected) {
  if (!path.isAbsolute(target) || fs.realpathSync(path.dirname(target)) !== path.dirname(target)) {
    throw new Error("frontend destination parent must be canonical and absolute");
  }
  if (!sameInventory(frontendInputs(frontend, node), expected)) {
    throw new Error("frontend inputs changed before preparation");
  }
  fs.mkdirSync(target, { mode: 0o700 }); // Exclusive: never replace a serving/failed image.
  fs.cpSync(path.join(frontend, ".next", "standalone"), path.join(target, "server"), {
    recursive: true, dereference: false, errorOnExist: true, force: false,
  });
  const server = path.join(target, "server");
  if (!sameInventory(inventory(server), expected.standalone)) throw new Error("standalone copy changed");
  for (const [name, source, destination] of [
    ["static", path.join(frontend, ".next", "static"), path.join(server, ".next", "static")],
    ["public", path.join(frontend, "public"), path.join(server, "public")],
  ]) {
    if (name === "public" && !fs.existsSync(source)) continue;
    if (fs.existsSync(destination)) throw new Error(`unexpected traced ${name} destination`);
    fs.cpSync(source, destination, { recursive: true, dereference: false, errorOnExist: true, force: false });
    if (!sameInventory(inventory(destination), expected[name])) throw new Error(`${name} copy changed`);
  }
  fs.copyFileSync(node, path.join(target, "node"), fs.constants.COPYFILE_EXCL);
  fs.chmodSync(path.join(target, "node"), 0o500);
  if (sha(fs.readFileSync(path.join(target, "node"))) !== expected.node) throw new Error("Node copy changed");
  const files = inventory(target);
  const manifest = {
    version: 1, nodeVersion: process.version, platform: process.platform, arch: process.arch,
    inputDigest: sha(JSON.stringify(expected)), files,
    publicBuildConfig: expected.publicBuildConfig,
  };
  fs.writeFileSync(path.join(target, "frontend-manifest.json"), JSON.stringify(manifest) + "\n", {
    flag: "wx", mode: 0o400,
  });
  return manifest;
}

export function verifyFrontend(target, expectedManifestHash) {
  const manifestPath = path.join(target, "frontend-manifest.json");
  const bytes = fs.readFileSync(manifestPath);
  if (sha(bytes) !== expectedManifestHash) throw new Error("frontend manifest changed");
  const manifest = JSON.parse(bytes);
  const files = inventory(target);
  delete files["frontend-manifest.json"];
  if (!sameInventory(files, manifest.files)) throw new Error("frontend image changed");
  return manifest;
}
