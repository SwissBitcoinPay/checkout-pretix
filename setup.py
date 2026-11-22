"""The build of the plugin: setuptools', with its translations compiled into the build.

pretix reads the compiled catalogs (.mo) only: each catalog of the sources
(pretix_sattleme/locale/<language>/LC_MESSAGES/django.po) is compiled next to its copy in the
build, by scripts/translations.py (Babel reads it; no GNU gettext needed).
"""

import importlib.util
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parent


def compile_catalog(path: Path) -> bytes:
    script = ROOT / "scripts" / "translations.py"
    spec = importlib.util.spec_from_file_location("translations", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    compiled: bytes = module.compile_catalog(path)
    return compiled


class BuildPy(build_py):
    def run(self) -> None:
        super().run()
        # An editable install runs from the sources, whose catalogs `make localecompile`
        # compiles in place.
        if self.dry_run or self.editable_mode:
            return
        for path in sorted(ROOT.glob("pretix_sattleme/locale/*/LC_MESSAGES/django.po")):
            target = Path(self.build_lib) / path.relative_to(ROOT).with_suffix(".mo")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(compile_catalog(path))


setup(cmdclass={"build_py": BuildPy})
