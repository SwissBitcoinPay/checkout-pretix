"""Check what `python -m build` made, before anyone publishes it: one wheel and one sdist of the
version the plugin declares, the wheel holding the plugin alone (its migrations, templates,
static files and compiled translations, its entry point for pretix), the sdist the sources and
the tests too, and the metadata of pyproject.toml. Neither holds the SDK pinned in sdk/, which
serves the development and the CI only: installations get it from PyPI, as a dependency. Each
translation of the wheel is its catalog of the sources, compiled (scripts/translations.py).

Usage: python scripts/check_dist.py dist/
"""

import email.parser
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path
from typing import NoReturn

from packaging.requirements import Requirement
from translations import compile_catalog

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "pretix_sattleme"
ENTRY_POINT = "pretix_sattleme = pretix_sattleme:PretixPluginMeta"


def fail(message: str) -> NoReturn:
    print(f"check_dist: {message}", file=sys.stderr)
    raise SystemExit(1)


def main(directory: Path) -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]
    version = project["version"]
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    expected_wheel = f"{PACKAGE}-{version}-py3-none-any.whl"
    expected_sdist = f"{PACKAGE}-{version}.tar.gz"
    if [w.name for w in wheels] != [expected_wheel]:
        fail(f"expected {expected_wheel} alone, found {[w.name for w in wheels]}")
    if [s.name for s in sdists] != [expected_sdist]:
        fail(f"expected {expected_sdist} alone, found {[s.name for s in sdists]}")

    # Every translation of the sources, compiled: pretix reads the .mo files only.
    sources = [
        p.relative_to(ROOT).as_posix()
        for p in sorted((ROOT / PACKAGE / "locale").glob("*/LC_MESSAGES/django.po"))
    ]
    if not sources:
        fail("no translations in the sources")
    compiled = [s[: -len(".po")] + ".mo" for s in sources]

    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
        info = f"{PACKAGE}-{version}.dist-info"
        outside = [n for n in names if not n.startswith((f"{PACKAGE}/", f"{info}/"))]
        if outside:
            fail(f"the wheel holds files outside the package: {outside}")
        for needed in (
            f"{PACKAGE}/__init__.py",
            f"{PACKAGE}/apps.py",
            f"{PACKAGE}/migrations/0001_initial.py",
            f"{PACKAGE}/templates/{PACKAGE}/pay.html",
            f"{PACKAGE}/static/{PACKAGE}/pay.js",
            f"{PACKAGE}/static/{PACKAGE}/pay.css",
            *compiled,
            f"{info}/licenses/LICENSE",
        ):
            if needed not in names:
                fail(f"the wheel lacks {needed}")
        stray = [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
        if stray:
            fail(f"the wheel holds compiled files: {stray}")
        others = [n for n in names if n.endswith(".mo") and n not in compiled]
        if others:
            fail(f"the wheel holds translations of no catalog: {others}")
        for source, mo in zip(sources, compiled):
            if wheel.read(mo) != compile_catalog(ROOT / source):
                fail(f"the wheel's {mo} is not its catalog compiled")
        sdk = [n for n in names if "sattleme_checkout" in n or n.startswith("sdk/")]
        if sdk:
            fail(f"the wheel holds the pinned SDK: {sdk}")
        metadata = email.parser.Parser().parsestr(
            wheel.read(f"{info}/METADATA").decode()
        )
        entry_points = wheel.read(f"{info}/entry_points.txt").decode()
    if metadata["Name"] != project["name"] or metadata["Version"] != version:
        fail(f"the wheel's metadata names {metadata['Name']} {metadata['Version']}")
    if metadata["License-Expression"] != project["license"]:
        fail(f"the wheel's license is {metadata['License-Expression']}")
    if metadata["Requires-Python"] != project["requires-python"]:
        fail(f"the wheel requires Python {metadata['Requires-Python']}")
    if metadata["Description-Content-Type"] != "text/markdown":
        fail(f"the wheel's description is {metadata['Description-Content-Type']}")
    # Compared as requirements: the metadata spells them as setuptools normalizes them.
    requires = sorted(
        str(Requirement(r)) for r in metadata.get_all("Requires-Dist") or []
    )
    if requires != sorted(str(Requirement(r)) for r in project["dependencies"]):
        fail(f"the wheel's dependencies are {requires}")
    # Without it, pretix never loads the plugin.
    if (
        "[pretix.plugin]" not in entry_points
        or ENTRY_POINT not in entry_points.splitlines()
    ):
        fail(f"the wheel lacks the entry point {ENTRY_POINT}")

    with tarfile.open(sdists[0]) as sdist:
        names = sdist.getnames()
    top = f"{PACKAGE}-{version}"
    for needed in (
        "pyproject.toml",
        "setup.py",
        "scripts/translations.py",
        "setup.cfg",
        "MANIFEST.in",
        "README.md",
        "LICENSE",
        "CHANGELOG.md",
        "SECURITY.md",
        f"{PACKAGE}/__init__.py",
        *sources,
        "tests/__init__.py",
        "tests/conftest.py",
    ):
        if f"{top}/{needed}" not in names:
            fail(f"the sdist lacks {needed}")
    # Compiled files are built from the sources, never shipped unseen in the sdist.
    stray = [n for n in names if "__pycache__" in n or n.endswith((".pyc", ".mo"))]
    if stray:
        fail(f"the sdist holds compiled files: {stray}")
    sdk = [n for n in names if n.startswith(f"{top}/sdk") or "sattleme_checkout" in n]
    if sdk:
        fail(f"the sdist holds the pinned SDK: {sdk}")
    print(f"check_dist: {expected_wheel} and {expected_sdist} are as they should be")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        fail("usage: python scripts/check_dist.py dist/")
    main(Path(sys.argv[1]))
