// Local distributor configuration is read into the child build environment only.
// It is never copied into the WebView, build logs, or source-controlled assets.
import { readFileSync, existsSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import {
  developmentIdentity,
  signDevelopment,
} from "./development-signing.mjs";
const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const configPath =
  process.env.PERMAFROST_GOOGLE_CLIENT_FILE ??
  resolve(root, "google-client-desktop.json");
const env = { ...process.env };
const args = process.argv.slice(2);
if (process.platform === "darwin") {
  developmentIdentity();
  env.MACOSX_DEPLOYMENT_TARGET ||= JSON.parse(
    readFileSync(resolve(root, "src-tauri/tauri.conf.json"), "utf8"),
  ).bundle.macOS.minimumSystemVersion;
  if (args[0] === "dev") {
    const target =
      process.arch === "arm64" ? "aarch64-apple-darwin" : "x86_64-apple-darwin";
    env.PERMAFROST_DEV_NODE = process.execPath;
    env.PERMAFROST_DEV_SIGN_SCRIPT = resolve(
      root,
      "tooling/tauri/sign-development-binary.mjs",
    );
    const runner = [
      "/bin/sh",
      resolve(root, "tooling/tauri/development-runner.sh"),
    ];
    const separator = args.indexOf("--");
    const cargoArgs = [
      "--config",
      `target.${target}.runner=${JSON.stringify(runner)}`,
    ];
    if (separator < 0) args.push("--", ...cargoArgs);
    else args.splice(separator + 1, 0, ...cargoArgs);
  }
}
if (existsSync(configPath)) {
  const config = JSON.parse(readFileSync(configPath, "utf8")).installed;
  if (
    !config?.client_id?.endsWith(".apps.googleusercontent.com") ||
    !config.client_secret
  )
    throw new Error(
      "Expected an approved Google Desktop OAuth client configuration.",
    );
  env.PERMAFROST_GOOGLE_CLIENT_ID = config.client_id;
  env.PERMAFROST_GOOGLE_CLIENT_SECRET = config.client_secret;
  console.log(
    "Building with the local Desktop OAuth configuration (contents are not logged).",
  );
}
const result = spawnSync(
  process.execPath,
  [resolve(root, "node_modules/@tauri-apps/cli/tauri.js"), ...args],
  { cwd: root, env, stdio: "inherit" },
);
if (result.error) throw result.error;
if (
  result.status === 0 &&
  process.platform === "darwin" &&
  args[0] === "build"
) {
  const targetIndex = args.indexOf("--target");
  const target = targetIndex >= 0 ? args[targetIndex + 1] : "";
  const profile = args.includes("--debug") ? "debug" : "release";
  signDevelopment(
    resolve(
      root,
      env.CARGO_TARGET_DIR || "target",
      target,
      profile,
      "bundle/macos/Permafrost.app",
    ),
  );
}
process.exit(result.status ?? 1);
