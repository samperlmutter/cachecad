import { readFileSync, writeFileSync } from "node:fs";
const release = process.argv.includes("--release");
const key = process.env.PERMAFROST_UPDATER_PUBLIC_KEY?.trim();
if (release && !key)
  throw new Error(
    "Set PERMAFROST_UPDATER_PUBLIC_KEY before building a release.",
  );
if (release && !process.env.TAURI_SIGNING_PRIVATE_KEY)
  throw new Error(
    "A signed update release requires TAURI_SIGNING_PRIVATE_KEY.",
  );
const config = JSON.parse(readFileSync("src-tauri/tauri.conf.json", "utf8"));
config.bundle.createUpdaterArtifacts = release;
if (key) {
  config.plugins ??= {};
  config.plugins.updater = {
    pubkey: key,
    requireSignedVersion: true,
    endpoints: [
      "https://github.com/samperlmutter/cachecad/releases/latest/download/latest.json",
    ],
    windows: { installMode: "passive" },
  };
}
// A distributor identity must replace the ad-hoc development identity.
if (process.env.APPLE_SIGNING_IDENTITY?.trim()) {
  config.bundle.macOS.signingIdentity =
    process.env.APPLE_SIGNING_IDENTITY.trim();
} else if (process.env.APPLE_CERTIFICATE) {
  delete config.bundle.macOS.signingIdentity;
}
if (release) {
  const version = JSON.parse(readFileSync("package.json", "utf8")).version;
  if (process.env.GITHUB_REF_NAME !== `v${version}`)
    throw new Error("Release tag must exactly match the application version.");
  if (config.version !== version)
    throw new Error("Tauri and npm versions differ.");
  const cargo = readFileSync("Cargo.toml", "utf8");
  if (!cargo.includes(`version = "${version}"`))
    throw new Error("Rust workspace version differs.");
}
writeFileSync(
  "src-tauri/tauri.conf.json",
  JSON.stringify(config, null, 2) + "\n",
);
