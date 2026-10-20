import {
  mkdirSync,
  mkdtempSync,
  writeFileSync,
  readFileSync,
  existsSync,
  rmSync,
  renameSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomBytes } from "node:crypto";
import { spawnSync } from "node:child_process";
import {
  signingDirectory,
  developmentIdentity,
} from "./development-signing.mjs";

if (process.platform !== "darwin")
  throw new Error("Local certificate setup is only needed on macOS.");
const identityFile = join(signingDirectory, "identity.json");
if (
  existsSync(identityFile) &&
  JSON.parse(readFileSync(identityFile, "utf8")).keychain
) {
  developmentIdentity();
  const config = JSON.parse(readFileSync(identityFile, "utf8"));
  const listed = spawnSync(
    "/usr/bin/security",
    ["list-keychains", "-d", "user"],
    { encoding: "utf8" },
  );
  if (listed.status !== 0)
    throw new Error("Could not read the local Keychain search list.");
  const paths = listed.stdout
    .trim()
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line.trim()));
  if (!paths.includes(config.keychain)) {
    const result = spawnSync("/usr/bin/security", [
      "list-keychains",
      "-d",
      "user",
      "-s",
      ...paths,
      config.keychain,
    ]);
    if (result.status !== 0)
      throw new Error("Could not register the dedicated development Keychain.");
  }
  console.log(
    "Reusing the existing Permafrost local development signing identity.",
  );
} else {
  mkdirSync(signingDirectory, { recursive: true, mode: 0o700 });
  const temporary = mkdtempSync(
    join(tmpdir(), "permafrost-development-signing-"),
  );
  const password = randomBytes(32).toString("hex");
  const unique = randomBytes(8).toString("hex");
  const keychain = join(signingDirectory, `development-${unique}.keychain-db`);
  const passwordFile = join(signingDirectory, `keychain-password-${unique}`);
  let created = false;
  let saved = false;
  const env = { ...process.env, PERMAFROST_TEMP_IMPORT_PASSWORD: password };
  function run(program, args) {
    const result = spawnSync(program, args, {
      env,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
    });
    if (result.error || result.status !== 0)
      throw new Error(
        `Local signing setup failed in ${program}; no credential contents were logged.`,
      );
    return result.stdout;
  }
  const originalSearchList = run("/usr/bin/security", [
    "list-keychains",
    "-d",
    "user",
  ])
    .trim()
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line.trim()));
  try {
    const key = join(temporary, "key.pem");
    const certificate = join(temporary, "certificate.pem");
    const archive = join(temporary, "identity.p12");
    const config = join(temporary, "certificate.cnf");
    writeFileSync(
      config,
      "[req]\nprompt=no\ndistinguished_name=name\nx509_extensions=extensions\n[name]\nCN=Permafrost Local Development\n[extensions]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=critical,codeSigning\n",
      { mode: 0o600 },
    );
    run("openssl", [
      "req",
      "-x509",
      "-newkey",
      "rsa:3072",
      "-sha256",
      "-nodes",
      "-days",
      "3650",
      "-config",
      config,
      "-keyout",
      key,
      "-out",
      certificate,
    ]);
    run("openssl", [
      "pkcs12",
      "-export",
      "-macalg",
      "sha1",
      "-certpbe",
      "PBE-SHA1-3DES",
      "-keypbe",
      "PBE-SHA1-3DES",
      "-inkey",
      key,
      "-in",
      certificate,
      "-name",
      "Permafrost Local Development",
      "-passout",
      "env:PERMAFROST_TEMP_IMPORT_PASSWORD",
      "-out",
      archive,
    ]);
    // This Keychain contains only this local signing identity. Its random
    // password stays mode 0600 beside it; builds unlock it only while signing.
    run("/usr/bin/security", ["create-keychain", "-p", password, keychain]);
    created = true;
    run("/usr/bin/security", ["set-keychain-settings", "-lut", "60", keychain]);
    run("/usr/bin/security", ["unlock-keychain", "-p", password, keychain]);
    run("/usr/bin/security", [
      "import",
      archive,
      "-k",
      keychain,
      "-P",
      password,
      "-x",
      "-T",
      "/usr/bin/codesign",
    ]);
    // Only this new key is affected. Never modify the login Keychain or the
    // Google session's access policy, and never add a trusted root certificate.
    run("/usr/bin/security", [
      "set-key-partition-list",
      "-S",
      "apple-tool:,apple:,codesign:",
      "-s",
      "-k",
      password,
      keychain,
    ]);
    const fingerprint = run("openssl", [
      "x509",
      "-in",
      certificate,
      "-noout",
      "-fingerprint",
      "-sha1",
    ])
      .trim()
      .split("=")[1]
      .replaceAll(":", "");
    writeFileSync(
      join(signingDirectory, "certificate.pem"),
      readFileSync(certificate),
      { mode: 0o600 },
    );
    writeFileSync(passwordFile, password, { mode: 0o600, flag: "wx" });
    const pending = join(signingDirectory, `identity-${unique}.json`);
    writeFileSync(
      pending,
      JSON.stringify(
        {
          identity: fingerprint,
          name: "Permafrost Local Development",
          keychain,
          passwordFile,
        },
        null,
        2,
      ) + "\n",
      { mode: 0o600, flag: "wx" },
    );
    renameSync(pending, identityFile);
    saved = true;
    console.log(
      "Created a persistent identity in a dedicated local development Keychain. Builds unlock only this signing key. Its private key is non-extractable; production signing and Google credentials are unchanged.",
    );
  } finally {
    try {
      run("/usr/bin/security", [
        "list-keychains",
        "-d",
        "user",
        "-s",
        ...originalSearchList,
        ...(saved ? [keychain] : []),
      ]);
      if (created) run("/usr/bin/security", ["lock-keychain", keychain]);
      if (created && !saved) {
        run("/usr/bin/security", ["delete-keychain", keychain]);
        rmSync(passwordFile, { force: true });
      }
    } finally {
      rmSync(temporary, { recursive: true, force: true });
    }
  }
}
