"""Take a version of sattleme-checkout, the SDK this plugin runs on, into sdk/.

    python3 scripts/pin_sdk.py <checkout-python clone> [<commit>]

From a clone of https://github.com/sattleme/checkout-python, at <commit> (a hash, a tag or a branch
of the clone; without one, the commit the clone is on, which must then be clean), and never from
its working tree. The commit must be on a branch of the clone's `origin`, as last fetched: never
one the clone alone has; the output names the clone and its origin. Copies the files the SDK's
package is built from (pyproject.toml, README.md, LICENSE and every file of
src/sattleme_checkout/), and the SDK's vectors of prices that the plugin's tests run
(spec/vectors/fiat.json), byte for byte, into sdk/ (replacing what it held); writes sdk/PIN.json
(the repository, the commit, the version, each file's SHA-256); then runs `uv lock`, since uv.lock
records the SDK's version and dependencies from its pyproject.toml.

It needs Python 3.11, git and uv alone (uv as `uv` on the PATH, or the path in $UV, of a version
that pyproject.toml's `required-version` accepts, as .github/ci-requirements.txt's): it runs from
a clean clone of this repository, before any environment is installed, and changes nothing when
something is missing. sdk/ serves the development and the CI only (pyproject.toml,
[tool.uv.sources]): the package declares the SDK as a dependency, and installations get it from
PyPI. The copy is never edited by hand (tests/test_sdk_pin.py checks it file by file). Then
reinstall it (`uv sync --locked --reinstall-package sattleme-checkout`: uv keeps the build it has
when sdk/pyproject.toml did not change), run the checks and the tests, and commit sdk/ and
uv.lock together.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parent.parent
SDK = ROOT / "sdk"
REPOSITORY = "https://github.com/sattleme/checkout-python"
NAME = "sattleme-checkout"
PACKAGE = "src/sattleme_checkout"
TOP_FILES = ("LICENSE", "README.md", "pyproject.toml")
# The SDK's own vectors of the specification that the plugin's tests run: the prices it takes.
VECTORS = ("spec/vectors/fiat.json",)


def fail(message: str) -> NoReturn:
    sys.exit(f"pin_sdk: {message}")


def git(clone: Path, *args: str) -> bytes:
    try:
        done = subprocess.run(
            ["git", "-C", str(clone), *args], check=True, capture_output=True
        )
    except subprocess.CalledProcessError as e:
        fail(f"git {' '.join(args)}: {e.stderr.decode(errors='replace').strip()}")
    return done.stdout


def resolve(clone: Path, commit: str | None) -> str:
    """The full hash of `commit` in the clone; of the commit it is on when None, which must
    then be clean (what would be imported is that commit, never the changes beside it).
    """
    if commit is None:
        if git(clone, "status", "--porcelain", "--untracked-files=no").strip():
            fail(f"{clone} has changes: commit them, or name the commit to import")
        commit = "HEAD"
    verify = ["rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"]
    found = subprocess.run(["git", "-C", str(clone), *verify], capture_output=True)
    resolved = found.stdout.decode().strip()
    if found.returncode or not re.fullmatch("[0-9a-f]{40}", resolved):
        fail(f"{commit} is not a commit of {clone}")
    return resolved


def upstream(clone: Path, commit: str) -> str:
    """The URL of the clone's `origin`, which must have `commit` on one of its branches (as the
    clone last fetched them): sdk/ never holds a commit that only a clone has."""
    found = subprocess.run(
        ["git", "-C", str(clone), "remote", "get-url", "origin"], capture_output=True
    )
    url = found.stdout.decode().strip()
    if found.returncode or not url:
        fail(f"{clone} has no remote `origin`: clone {REPOSITORY}")
    branches = git(
        clone,
        "for-each-ref",
        "--contains",
        commit,
        "--format=%(refname)",
        "refs/remotes/origin/",
    )
    if not branches.strip():
        fail(
            f"{commit} is on no branch of {clone}'s origin ({url}), as last fetched: push it "
            "there, or fetch origin, first"
        )
    return url


INSTALL_UV = (
    "install the CI's (python3 -m pip install --require-hashes --only-binary :all: --no-deps "
    "-r .github/ci-requirements.txt), or give its path in $UV"
)


def numbers(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def uv() -> str:
    """The uv that updates uv.lock, of a version pyproject.toml's `required-version` accepts:
    checked before anything changes (uv itself would refuse only once sdk/ was copied).
    """
    found = os.environ.get("UV") or shutil.which("uv")
    if not found:
        fail(f"uv is needed to update uv.lock: {INSTALL_UV}")
    settings = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    required = str(settings["tool"]["uv"].get("required-version", "")).strip()
    floor = re.fullmatch(r">=\s*([0-9]+(?:\.[0-9]+)*)", required)
    if floor:
        answer = subprocess.run([found, "--version"], capture_output=True, text=True)
        running = re.search(r"[0-9]+(?:\.[0-9]+)+", answer.stdout)
        if running is None or numbers(running.group()) < numbers(floor.group(1)):
            seen = running.group() if running else "of an unknown version"
            fail(
                f"{found} is uv {seen}, pyproject.toml asks for uv {required}: {INSTALL_UV}"
            )
    return str(found)


def main(clone: Path, commit: str | None) -> None:
    uv_path = uv()
    commit = resolve(clone, commit)
    origin = upstream(clone, commit)
    print(f"pin_sdk: reading {clone.resolve()} (origin {origin}) at {commit}")
    listed = git(clone, "ls-tree", "-r", "-z", "--name-only", commit, "--", PACKAGE)
    package = sorted(p for p in listed.decode().split("\0") if p)
    if f"{PACKAGE}/__init__.py" not in package:
        fail(f"{commit} has no {PACKAGE}/__init__.py")
    listed = git(clone, "ls-tree", "-r", "-z", "--name-only", commit, "--", *VECTORS)
    missing = sorted(set(VECTORS) - set(listed.decode().split("\0")))
    if missing:
        fail(f"{commit} has no {', '.join(missing)}")
    contents = {
        path: git(clone, "show", f"{commit}:{path}") for path in (*TOP_FILES, *VECTORS)
    }
    contents.update({path: git(clone, "show", f"{commit}:{path}") for path in package})
    project = tomllib.loads(contents["pyproject.toml"].decode("utf-8"))["project"]
    if project.get("name") != NAME:
        fail(f"{commit} is not a commit of {NAME}, but of {project.get('name')}")
    version = project.get("version")
    if not isinstance(version, str):
        fail(f"{commit}: its pyproject.toml declares no static version")

    if SDK.exists():
        shutil.rmtree(SDK)
    for path, data in contents.items():
        target = SDK / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    pin = {
        "repository": REPOSITORY,
        "commit": commit,
        "version": version,
        "files": {
            path: hashlib.sha256(data).hexdigest()
            for path, data in sorted(contents.items())
        },
    }
    (SDK / "PIN.json").write_text(
        json.dumps(pin, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(f"pin_sdk: sdk/ holds {NAME} {version} ({commit}, {len(contents)} files)")

    locked = subprocess.run([uv_path, "lock"], cwd=ROOT)
    if locked.returncode:
        fail(f"`{uv_path} lock` failed: sdk/ is updated, uv.lock is not")
    print(
        "pin_sdk: uv.lock is up to date; now reinstall the SDK (uv sync --locked "
        "--reinstall-package sattleme-checkout, with your groups), run the checks and the "
        "tests, then commit sdk/ and uv.lock"
    )


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    main(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) == 3 else None)
