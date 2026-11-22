"""The pinned copy of the SDK in sdk/ (scripts/pin_sdk.py): what the development and the CI
install (pyproject.toml, [tool.uv.sources]), never what is distributed."""

import hashlib
import json
import re
import subprocess
import tomllib
from pathlib import Path

import pin_sdk
import pytest
import sattleme_checkout
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent
SDK = ROOT / "sdk"


def pin():
    with (SDK / "PIN.json").open(encoding="utf-8") as f:
        return json.load(f)


def project():
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def sdk_project():
    return tomllib.loads((SDK / "pyproject.toml").read_text(encoding="utf-8"))


def test_pin_names_one_commit_of_checkout_python():
    data = pin()
    assert data["repository"] == "https://github.com/sattleme/checkout-python"
    assert re.fullmatch("[0-9a-f]{40}", data["commit"])
    assert str(Version(data["version"])) == data["version"]


def test_every_file_has_its_sha256_and_nothing_else_is_there():
    files = pin()["files"]
    for relative, digest in files.items():
        assert (
            hashlib.sha256((SDK / relative).read_bytes()).hexdigest() == digest
        ), relative
    present = {p.relative_to(SDK).as_posix() for p in SDK.rglob("*") if p.is_file()}
    assert present == {*files, "PIN.json"}
    assert "pyproject.toml" in files and "src/sattleme_checkout/__init__.py" in files
    # The SDK's vectors of prices, which the plugin's check of a price runs.
    assert "spec/vectors/fiat.json" in files


def test_the_package_declares_a_range_that_holds_the_pinned_version():
    """Semantic versioning: the package takes the SDK's 1.x releases, which keep its API; the
    pinned copy is one of them, the one the development and the CI install.
    """
    [requirement] = [
        Requirement(r)
        for r in project()["project"]["dependencies"]
        if Requirement(r).name == "sattleme-checkout"
    ]
    assert str(requirement.specifier) == "<2,>=1.0"
    assert requirement.specifier.contains(pin()["version"])
    assert sdk_project()["project"]["name"] == "sattleme-checkout"
    assert sdk_project()["project"]["version"] == pin()["version"]
    assert project()["tool"]["uv"]["sources"]["sattleme-checkout"] == {"path": "sdk"}


def test_the_lockfile_holds_the_sdks_build_backend():
    """uv builds the pinned copy without isolation, with the development group's backend: the
    one the SDK's pyproject.toml asks for."""
    [backend] = [Requirement(r) for r in sdk_project()["build-system"]["requires"]]
    pinned = {
        canonicalize_name(Requirement(r).name): Requirement(r).specifier
        for r in project()["dependency-groups"]["dev"]
    }
    [exact] = list(pinned[canonicalize_name(backend.name)])
    assert exact.operator == "==" and backend.specifier.contains(exact.version)
    assert "sattleme-checkout" in project()["tool"]["uv"]["no-build-isolation-package"]


def test_the_tests_run_the_pinned_copy():
    assert sattleme_checkout.__version__ == pin()["version"]
    installed = Path(sattleme_checkout.__file__).resolve().parent
    files = {
        path.removeprefix("src/sattleme_checkout/"): digest
        for path, digest in pin()["files"].items()
        if path.startswith("src/sattleme_checkout/")
    }
    for name, digest in files.items():
        assert (
            hashlib.sha256((installed / name).read_bytes()).hexdigest() == digest
        ), name


def run_git(directory, *args):
    subprocess.run(
        [
            "git",
            "-C",
            str(directory),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.org",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "tag.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def sdk_clone(tmp_path, monkeypatch):
    """A clone of a repository holding an SDK as checkout-python does; scripts/pin_sdk.py's
    target is a directory of the test's, its `uv lock` a command that does nothing."""
    origin = tmp_path / "origin"
    (origin / "src" / "sattleme_checkout").mkdir(parents=True)
    (origin / "pyproject.toml").write_text(
        '[project]\nname = "sattleme-checkout"\nversion = "1.0.0"\n', encoding="utf-8"
    )
    (origin / "README.md").write_text("The SDK\n", encoding="utf-8")
    (origin / "LICENSE").write_text("MIT\n", encoding="utf-8")
    (origin / "src" / "sattleme_checkout" / "__init__.py").write_text(
        '__version__ = "1.0.0"\n', encoding="utf-8"
    )
    (origin / "spec" / "vectors").mkdir(parents=True)
    (origin / "spec" / "vectors" / "fiat.json").write_text(
        '{"cases": [], "digits": []}\n', encoding="utf-8"
    )
    run_git(tmp_path, "init", "-q", "-b", "main", str(origin))
    run_git(origin, "config", "receive.denyCurrentBranch", "updateInstead")
    run_git(origin, "add", "-A")
    run_git(origin, "commit", "-q", "-m", "SDK")
    clone = tmp_path / "clone"
    run_git(tmp_path, "clone", "-q", str(origin), str(clone))
    root = tmp_path / "plugin"
    root.mkdir()
    (root / "pyproject.toml").write_text("[tool.uv]\n", encoding="utf-8")
    monkeypatch.setattr(pin_sdk, "ROOT", root)
    monkeypatch.setattr(pin_sdk, "SDK", root / "sdk")
    monkeypatch.setattr(pin_sdk, "uv", lambda: "true")
    return clone


def test_pin_sdk_says_which_clone_it_read(sdk_clone, capsys):
    pin_sdk.main(sdk_clone, None)
    out = capsys.readouterr().out
    assert (
        f"reading {sdk_clone.resolve()} (origin {sdk_clone.parent / 'origin'})" in out
    )
    pinned = json.loads((pin_sdk.SDK / "PIN.json").read_text(encoding="utf-8"))
    assert pinned["version"] == "1.0.0"
    assert set(pinned["files"]) == {
        "LICENSE",
        "README.md",
        "pyproject.toml",
        "src/sattleme_checkout/__init__.py",
        "spec/vectors/fiat.json",
    }
    assert (pin_sdk.SDK / "spec" / "vectors" / "fiat.json").read_text(
        encoding="utf-8"
    ) == '{"cases": [], "digits": []}\n'


def test_pin_sdk_refuses_a_commit_without_the_vectors_of_prices(sdk_clone):
    run_git(sdk_clone, "rm", "-q", "spec/vectors/fiat.json")
    run_git(sdk_clone, "commit", "-q", "-m", "No vectors")
    run_git(sdk_clone, "push", "-q", "origin", "main")
    with pytest.raises(SystemExit, match="has no spec/vectors/fiat.json"):
        pin_sdk.main(sdk_clone, None)
    assert not pin_sdk.SDK.exists()


def test_pin_sdk_refuses_a_commit_its_origin_does_not_have(sdk_clone, capsys):
    """A commit of the clone alone (never pushed, or of another repository's fetched into it):
    sdk/ would hold an SDK no one else can see. Nothing changes."""
    (sdk_clone / "README.md").write_text("Changed here only\n", encoding="utf-8")
    run_git(sdk_clone, "commit", "-q", "-am", "Local")
    with pytest.raises(SystemExit, match="on no branch of .*'s origin"):
        pin_sdk.main(sdk_clone, None)
    assert not pin_sdk.SDK.exists()

    # Once origin has it, as last fetched, it is taken.
    run_git(sdk_clone, "push", "-q", "origin", "main")
    pin_sdk.main(sdk_clone, None)
    assert (pin_sdk.SDK / "README.md").read_text(
        encoding="utf-8"
    ) == "Changed here only\n"


def test_pin_sdk_refuses_a_clone_without_origin(sdk_clone):
    run_git(sdk_clone, "remote", "remove", "origin")
    with pytest.raises(SystemExit, match="has no remote `origin`"):
        pin_sdk.main(sdk_clone, None)
    assert not pin_sdk.SDK.exists()


def test_pin_sdk_and_the_readme_say_to_reinstall_the_sdk(sdk_clone, capsys):
    """uv keeps the SDK it built before in an environment when sdk/pyproject.toml did not
    change (test_the_tests_run_the_pinned_copy catches it): reinstalled after a new pin.
    """
    command = "uv sync --locked --reinstall-package sattleme-checkout"
    pin_sdk.main(sdk_clone, None)
    assert command in capsys.readouterr().out
    assert command in " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
