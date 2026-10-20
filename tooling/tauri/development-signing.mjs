// A persistent local certificate authenticates rebuilt apps to the fixed local
// credential helper. Login-Keychain partitions still bind self-signed code to
// individual builds; a stable requirement alone does not prevent their prompts.
// This does not confer Developer ID or notarization.
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

export const signingDirectory = join(
  homedir(),
  ".config/permafrost/development-signing",
);
export function developmentIdentity() {
  const configured = process.env.PERMAFROST_DEV_SIGNING_IDENTITY;
  const file = join(signingDirectory, "identity.json");
  const identity =
    configured ||
    (existsSync(file) && JSON.parse(readFileSync(file, "utf8")).identity);
  if (!identity || !/^[a-f0-9]{40}$/i.test(identity))
    throw new Error(
      "Run npm run desktop:setup-signing once to give local Mac builds a stable Keychain identity.",
    );
  return identity;
}
export function signDevelopment(
  target,
  identifier = "com.permafrost.desktop",
  hardened = false,
) {
  const file = join(signingDirectory, "identity.json");
  const config = existsSync(file) ? JSON.parse(readFileSync(file, "utf8")) : {};
  const keychain =
    !process.env.PERMAFROST_DEV_SIGNING_IDENTITY && config.keychain;
  if (keychain) {
    const password = readFileSync(config.passwordFile, "utf8");
    const unlocked = spawnSync(
      "/usr/bin/security",
      ["unlock-keychain", "-p", password, keychain],
      { stdio: "pipe", timeout: 15000 },
    );
    if (unlocked.error || unlocked.status !== 0)
      throw new Error(
        "Could not unlock the dedicated development signing Keychain.",
      );
  }
  try {
    const result = spawnSync(
      "/usr/bin/codesign",
      [
        "--force",
        "--deep",
        "--sign",
        developmentIdentity(),
        "--identifier",
        identifier,
        "--timestamp=none",
        "--preserve-metadata=entitlements,flags",
        ...(hardened ? ["--options", "runtime"] : []),
        ...(keychain ? ["--keychain", keychain] : []),
        target,
      ],
      { stdio: "inherit", timeout: 30000 },
    );
    if (result.error || result.status !== 0)
      throw new Error("Local development signing failed.");
    const verified = spawnSync(
      "/usr/bin/codesign",
      ["--verify", "--deep", "--strict", target],
      { stdio: "inherit" },
    );
    if (verified.error || verified.status !== 0)
      throw new Error("Local development signature verification failed.");
  } finally {
    if (keychain)
      spawnSync("/usr/bin/security", ["lock-keychain", keychain], {
        stdio: "pipe",
        timeout: 15000,
      });
  }
}
