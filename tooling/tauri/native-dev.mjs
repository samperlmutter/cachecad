// Local distributor configuration is read into the child build environment only.
// It is never copied into the WebView, build logs, or source-controlled assets.
import { readFileSync, existsSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const configPath =
  process.env.PERMAFROST_GOOGLE_CLIENT_FILE ??
  resolve(root, "google-client-desktop.json");
const env = { ...process.env };
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
  [
    resolve(root, "node_modules/@tauri-apps/cli/tauri.js"),
    ...process.argv.slice(2),
  ],
  { cwd: root, env, stdio: "inherit" },
);
if (result.error) throw result.error;
process.exit(result.status ?? 1);
