// Build-time only. Installed users run the bundled runtime, never a system Python.
import {
  readFileSync,
  existsSync,
  mkdirSync,
  writeFileSync,
  readdirSync,
  cpSync,
} from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const output = resolve(root, "src-tauri/resources/cad-helper");
const executable = resolve(
  output,
  "permafrost-cad",
  `permafrost-cad${process.platform === "win32" ? ".exe" : ""}`,
);
const hash = createHash("sha256").update(
  `${process.platform}:${process.arch}:python3.12:v1`,
);
hash.update(process.env.APPLE_SIGNING_IDENTITY || "adhoc");
for (const name of ["convert.py", "requirements.txt", "build.mjs"])
  hash.update(readFileSync(resolve(root, "tooling/cad", name)));
for (const name of readdirSync(resolve(root, "tooling/cad/licenses")).sort())
  hash.update(readFileSync(resolve(root, "tooling/cad/licenses", name)));
const key = hash.digest("hex");
const manifest = resolve(output, "build.json");
function run(command, args) {
  const result = spawnSync(command, args, { cwd: root, stdio: "inherit" });
  if (result.error || result.status !== 0)
    throw Error("Native CAD runtime build failed.");
}
if (
  existsSync(manifest) &&
  existsSync(executable) &&
  JSON.parse(readFileSync(manifest)).key === key
) {
  console.log("Reusing the bundled native CAD runtime.");
  process.exit(0);
}
// Rust's target cache prunes non-Rust build files. Keep the Python environment
// in npm's ignored cache so restored Cargo artifacts cannot leave a partial pip.
const environment = resolve(root, "node_modules/.cache/permafrost-cad-env");
const environmentPython = resolve(
  environment,
  process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
);
let python = process.env.PERMAFROST_CAD_PYTHON;
if (!python) {
  if (!existsSync(environmentPython)) {
    if (spawnSync("uv", ["--version"], { stdio: "ignore" }).status === 0)
      run("uv", ["venv", "--python", "3.12", environment]);
    else {
      const system = process.platform === "win32" ? "python" : "python3";
      const version = spawnSync(
        system,
        ["-c", "import sys;sys.exit(sys.version_info[:2] != (3,12))"],
        { stdio: "ignore" },
      );
      if (version.status !== 0)
        throw Error(
          "Install Python 3.12 or uv, then run npm run desktop:setup-cad.",
        );
      run(system, ["-m", "venv", environment]);
    }
  }
  python = environmentPython;
}
if (spawnSync("uv", ["--version"], { stdio: "ignore" }).status === 0)
  run("uv", [
    "pip",
    "install",
    "--python",
    python,
    "--require-hashes",
    "--only-binary=:all:",
    "-r",
    "tooling/cad/requirements.txt",
  ]);
else
  run(python, [
    "-m",
    "pip",
    "install",
    "--require-hashes",
    "--only-binary=:all:",
    "-r",
    "tooling/cad/requirements.txt",
  ]);
mkdirSync(output, { recursive: true });
const args = [
  "-m",
  "PyInstaller",
  "--noconfirm",
  "--clean",
  "--onedir",
  "--name",
  "permafrost-cad",
  "--distpath",
  output,
  "--workpath",
  resolve(root, "target/cad-pyinstaller"),
  "--specpath",
  resolve(root, "target/cad-spec"),
  "--collect-all",
  "OCP",
];
if (process.platform === "darwin" && process.env.APPLE_SIGNING_IDENTITY)
  args.push("--codesign-identity", process.env.APPLE_SIGNING_IDENTITY);
args.push("tooling/cad/convert.py");
run(python, args);
const protocol = spawnSync(executable, ["--protocol"], { encoding: "utf8" });
if (
  protocol.status !== 0 ||
  protocol.stdout.trim() !== "permafrost-occt-7.9.3-merged-v1"
)
  throw Error("The bundled CAD runtime did not start correctly.");
// Retain redistribution notices alongside the dynamically linked runtime.
cpSync(resolve(root, "tooling/cad/licenses"), resolve(output, "licenses"), {
  recursive: true,
});
run(python, [
  "-c",
  `import importlib.metadata as m,json,pathlib,shutil
out=pathlib.Path(${JSON.stringify(output)})/'licenses';out.mkdir(exist_ok=True)
for name in ('cadquery-ocp-novtk','cadquery-ocp-proxy','pyinstaller'):
 d=m.distribution(name)
 for p in d.files or []:
  if any(s in p.name.lower() for s in ('license','copying','notice')):
   source=d.locate_file(p)
   if source.is_file():shutil.copyfile(source,out/(name+'-'+p.name))
(out/'sources.json').write_text(json.dumps({'OpenCascade':'https://github.com/Open-Cascade-SAS/OCCT/tree/V7_9_3','OCP':'https://github.com/CadQuery/OCP','build':'https://github.com/CadQuery/ocp-build-system','PyInstaller':'https://github.com/pyinstaller/pyinstaller'},indent=2))
`,
]);
writeFileSync(
  manifest,
  JSON.stringify({ key, protocol: protocol.stdout.trim() }, null, 2),
);
