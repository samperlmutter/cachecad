import {
  existsSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
  renameSync,
  chmodSync,
  rmSync,
} from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { createHash, randomBytes } from "node:crypto";
import { spawnSync } from "node:child_process";
import {
  signingDirectory,
  signDevelopment,
  developmentIdentity,
} from "./development-signing.mjs";

export function developmentKeychain() {
  const source = join(
    dirname(fileURLToPath(import.meta.url)),
    "development-keychain.swift",
  );
  const fingerprint = createHash("sha256")
    .update(readFileSync(source))
    .update(developmentIdentity())
    .digest("hex");
  const directory = join(signingDirectory, `credentials-${fingerprint}`);
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  const executable = join(directory, "Permafrost Development Credentials");
  const manifest = `${executable}.json`;
  const digest = () =>
    createHash("sha256").update(readFileSync(executable)).digest("hex");
  function codeHash() {
    const result = spawnSync(
      "/usr/bin/codesign",
      ["-d", "--verbose=4", executable],
      { encoding: "utf8" },
    );
    const cdhash = `${result.stdout}${result.stderr}`.match(
      /^CDHash=([a-f0-9]{40})$/m,
    )?.[1];
    if (result.status !== 0 || !cdhash)
      throw Error(
        "Could not verify the development credential helper identity.",
      );
    return cdhash;
  }
  if (existsSync(executable) && existsSync(manifest)) {
    const saved = JSON.parse(readFileSync(manifest, "utf8"));
    if (saved.sha256 !== digest())
      throw Error(
        `The development credential helper changed. Remove its generated directory (${directory}) and rebuild to regenerate it.`,
      );
    return { path: executable, sha256: saved.sha256, cdhash: codeHash() };
  }
  const pending = `${executable}-${randomBytes(8).toString("hex")}`;
  try {
    const result = spawnSync(
      "xcrun",
      [
        "swiftc",
        "-O",
        "-target",
        `${process.arch === "arm64" ? "arm64" : "x86_64"}-apple-macosx12.0`,
        source,
        "-o",
        pending,
      ],
      { encoding: "utf8", timeout: 120000 },
    );
    if (result.status !== 0 || result.error) {
      throw Error(
        `Could not compile the development credential helper: ${result.stderr}`,
      );
    }
    signDevelopment(
      pending,
      "com.permafrost.desktop.development-keychain",
      true,
    );
    chmodSync(pending, 0o500);
    renameSync(pending, executable);
    const sha256 = digest();
    writeFileSync(manifest, JSON.stringify({ sha256 }) + "\n", { mode: 0o600 });
    return { path: executable, sha256, cdhash: codeHash() };
  } finally {
    rmSync(pending, { force: true });
  }
}
