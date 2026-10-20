// Real login-Keychain I/O using generated test data, authenticated callers,
// different app binaries, and a helper whose bytes remain unchanged.
import { mkdtempSync, rmSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { randomBytes, createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { signDevelopment } from "./development-signing.mjs";
import { developmentKeychain } from "./development-keychain.mjs";

if (process.platform !== "darwin")
  throw Error("Run credential verification on macOS.");
const tooling = dirname(fileURLToPath(import.meta.url));
const root = mkdtempSync(join(tmpdir(), "permafrost credential qa "));
const helper = developmentKeychain();
const account = `permafrost-qa-${randomBytes(16).toString("hex")}.apps.googleusercontent.com`;
const value =
  'Disposable Permafrost credential regression; no Google access.\n"Permafrost" 🧊 零件';
function call(executable, operation, contents) {
  const result = spawnSync(executable, [helper.path], {
    input: JSON.stringify({
      operation,
      account,
      value: contents,
      interactive: false,
    }),
    encoding: "utf8",
    timeout: 15000,
  });
  if (result.error || result.status !== 0)
    throw Error("Credential test process failed.");
  if (!result.stdout.startsWith("PF1\n"))
    throw Error("The credential helper readiness handshake was missing.");
  return JSON.parse(result.stdout.slice(4));
}
const trusted = join(root, "app-1");
let created = false;
try {
  for (const version of [1, 2, 3]) {
    const executable = join(root, `app-${version}`);
    const compiled = spawnSync(
      "cc",
      [
        join(tooling, "development-keychain-fixture.c"),
        `-DVERSION=${version}`,
        "-o",
        executable,
      ],
      { encoding: "utf8" },
    );
    if (compiled.status !== 0)
      throw Error("Could not compile credential test caller.");
    signDevelopment(
      executable,
      version === 3
        ? "com.permafrost.untrusted-fixture"
        : "com.permafrost.desktop",
    );
  }
  if (call(trusted, "set", value).status !== 0)
    throw Error("Could not create the disposable credential.");
  created = true;
  for (const version of [1, 2]) {
    const response = call(join(root, `app-${version}`), "get");
    if (response.status !== 0 || response.value !== value)
      throw Error("Rebuilt caller could not restore its credential.");
  }
  for (const operation of ["get", "set", "delete"]) {
    if (
      call(join(root, "app-3"), operation, "Unauthorized replacement")
        .status !== -25293
    )
      throw Error(`An unrelated caller was not rejected for ${operation}.`);
  }
  if (call(helper.path, "get").status !== -25293)
    throw Error(
      "Direct helper invocation by an unrelated process was not rejected.",
    );
  const unsigned = join(root, "app-unsigned");
  const compiled = spawnSync("cc", [
    join(tooling, "development-keychain-fixture.c"),
    "-DVERSION=4",
    "-o",
    unsigned,
  ]);
  const signed = spawnSync("/usr/bin/codesign", [
    "--force",
    "--sign",
    "-",
    "--identifier",
    "com.permafrost.desktop",
    unsigned,
  ]);
  if (
    compiled.status !== 0 ||
    signed.status !== 0 ||
    call(unsigned, "get").status !== -25293
  )
    throw Error(
      "A caller with the right identifier but no trusted certificate was not rejected.",
    );
  if (
    call(trusted, "set", value + " Updated.").status !== 0 ||
    call(join(root, "app-2"), "get").value !== value + " Updated."
  )
    throw Error("Credential replacement did not survive a caller rebuild.");
  if (
    createHash("sha256").update(readFileSync(helper.path)).digest("hex") !==
    helper.sha256
  )
    throw Error("The credential helper changed during testing.");
  console.log(
    "Two different Permafrost binaries restored and updated one disposable login-Keychain credential; unrelated and ad-hoc callers were rejected.",
  );
} finally {
  try {
    if (created && call(trusted, "delete").status !== 0)
      throw Error("Could not remove the disposable test credential.");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}
