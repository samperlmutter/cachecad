// Exercise real signing and the Cargo runner with two different Mach-O binaries.
// Stable requirements identify callers; verify_development_keychain.mjs also
// exercises the separate login-Keychain partition gate with actual stored data.
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { signDevelopment } from "./development-signing.mjs";

if (process.platform !== "darwin")
  throw new Error("Run signing verification on macOS.");
const tooling = dirname(fileURLToPath(import.meta.url));
const root = mkdtempSync(join(tmpdir(), "permafrost signing qa "));
const requirements = [];
const hashes = [];
function run(program, args, options = {}) {
  const result = spawnSync(program, args, {
    encoding: "utf8",
    timeout: 30000,
    ...options,
  });
  if (result.error || result.status !== 0)
    throw new Error(`Development signing verification failed in ${program}.`);
  return result.stdout + result.stderr;
}
try {
  for (const version of [1, 2]) {
    const source = join(root, `version-${version}.c`);
    const executable = join(root, `version-${version}`);
    writeFileSync(
      source,
      `volatile int version = ${version}; int main(void) { return version == ${version} ? 0 : 1; }\n`,
    );
    run("cc", [source, "-o", executable]);
    signDevelopment(executable);
    // Signing through the exact runner must also be noninteractive and preserve
    // argument boundaries when the executable path contains spaces.
    run("/bin/sh", [join(tooling, "development-runner.sh"), executable], {
      env: {
        ...process.env,
        PERMAFROST_DEV_NODE: process.execPath,
        PERMAFROST_DEV_SIGN_SCRIPT: join(
          tooling,
          "sign-development-binary.mjs",
        ),
      },
    });
    requirements.push(
      run("/usr/bin/codesign", ["-d", "-r-", executable])
        .split("\n")
        .find((line) => line.startsWith("designated =>")),
    );
    hashes.push(
      createHash("sha256").update(readFileSync(executable)).digest("hex"),
    );
  }
  if (
    !requirements[0] ||
    requirements[0] !== requirements[1] ||
    hashes[0] === hashes[1]
  )
    throw new Error(
      "Rebuilt applications did not retain the same designated requirement.",
    );
  console.log(
    "Two different binaries signed, verified, and ran with one stable designated requirement and no prompts.",
  );
} finally {
  rmSync(root, { recursive: true, force: true });
}
