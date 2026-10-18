"""Install a local wheel offline in a fresh venv and test without the source tree."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import venv

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory(prefix="cachecad-wheel-check-") as temp:
        work = Path(temp)
        environment = work / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

        def run(*arguments):
            subprocess.run([str(python), *arguments], cwd=work, env=env, check=True)

        run("-m", "pip", "install", "--no-index", "--no-deps", str(wheel))
        run("-c", "import cachecad; from pathlib import Path; "
            "p=Path(cachecad.__file__).resolve(); print('Installed package:', p); "
            "assert 'site-packages' in p.parts")
        run("-m", "cachecad", "--demo", "--json")
        for directory in ["tests", "tooling", "tools/recovery"]:
            shutil.copytree(ROOT / directory, work / directory,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(ROOT / "version.json", work / "version.json")
        run("-m", "unittest", "discover", "-s", "tests", "-v")
        run("-m", "unittest", "discover", "-s", "tools/recovery/tests", "-v")
    print("Clean offline wheel installation, demo and copied test suites passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
