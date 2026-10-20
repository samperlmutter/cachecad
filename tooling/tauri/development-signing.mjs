// A persistent local certificate gives rebuilds the same designated requirement.
// Ad-hoc signatures use a different code hash on every build, which repeatedly
// asks for Keychain access. This does not confer Developer ID or notarization.
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
export function signDevelopment(target) {
  const result = spawnSync(
    "/usr/bin/codesign",
    [
      "--force",
      "--deep",
      "--sign",
      developmentIdentity(),
      "--identifier",
      "com.permafrost.desktop",
      "--timestamp=none",
      "--preserve-metadata=entitlements,flags",
      target,
    ],
    { stdio: "inherit" },
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
}
