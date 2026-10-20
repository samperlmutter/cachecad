import {
  mkdirSync,
  mkdtempSync,
  writeFileSync,
  readFileSync,
  existsSync,
  rmSync,
} from "node:fs";
import { tmpdir, homedir } from "node:os";
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
if (existsSync(identityFile)) {
  developmentIdentity();
  console.log(
    "Reusing the existing Permafrost local development signing identity.",
  );
} else {
  mkdirSync(signingDirectory, { recursive: true, mode: 0o700 });
  const temporary = mkdtempSync(
    join(tmpdir(), "permafrost-development-signing-"),
  );
  const password = randomBytes(32).toString("hex");
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
    // Restrict this new signing key to Apple's codesign tool. Never broaden the
    // Google credential ACL, modify other keys, or add a trusted root certificate.
    run("/usr/bin/security", [
      "import",
      archive,
      "-k",
      join(homedir(), "Library/Keychains/login.keychain-db"),
      "-P",
      password,
      "-x",
      "-T",
      "/usr/bin/codesign",
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
    writeFileSync(
      identityFile,
      JSON.stringify(
        { identity: fingerprint, name: "Permafrost Local Development" },
        null,
        2,
      ) + "\n",
      { mode: 0o600, flag: "wx" },
    );
    console.log(
      "Created the persistent local signing identity. On first signing, approve codesign's request for this key with Always Allow. The private key stays non-extractable in your login Keychain.",
    );
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
}
