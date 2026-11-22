"""The plugin's translations: its catalogs (pretix_sattleme/locale/<language>/LC_MESSAGES/
django.po) compiled into the .mo files pretix reads, without GNU gettext.

    python scripts/translations.py compile [catalog.po ...]
        Write each catalog's .mo next to it (every catalog of the plugin by default).
    python scripts/translations.py compare [catalog.po ...]
        Check that each .mo is the one GNU msgfmt writes of its catalog (needs msgfmt).

The build (setup.py) compiles them into the wheel with `compile_catalog`. Babel, pinned in the
lockfile, reads a catalog; its .mo is written here in GNU's format (GNU gettext's manual, "The
Format of GNU MO Files") as msgfmt writes it, byte for byte, without the optional hash table
(`msgfmt --no-hash`), which readers do not need: the header as the catalog writes it, but its
POT-Creation-Date (msgfmt leaves it out, so that a build is the same whenever the template was
made), then every message translated and not fuzzy, ordered by its bytes.
"""

from __future__ import annotations

import ast
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from babel.messages.pofile import read_po

ROOT = Path(__file__).resolve().parent.parent
LOCALE = ROOT / "pretix_sattleme" / "locale"
# A .mo file's magic number, and the size of its header: seven 32-bit integers.
MAGIC = 0x950412DE
HEADER_SIZE = 7 * 4
# The header's entry (its msgid is empty), and its string, one quoted line after another.
HEADER_ENTRY = re.compile(r'^msgid ""\nmsgstr ((?:".*"\n)+)', re.MULTILINE)
POT_CREATION_DATE = re.compile(r"^POT-Creation-Date:.*\n", re.MULTILINE)


def catalogs() -> list[Path]:
    """Every catalog of the plugin."""
    return sorted(LOCALE.glob("*/LC_MESSAGES/django.po"))


def _header(text: str, path: Path) -> str:
    """The header of a catalog, as its .po writes it. Its quoted lines are C strings, which
    Python's string literals read alike (as CPython's own Tools/i18n/msgfmt.py does)."""
    entry = HEADER_ENTRY.search(text)
    if entry is None:
        raise ValueError(f"{path}: no header")
    lines = entry.group(1).splitlines()
    return "".join(str(ast.literal_eval(line)) for line in lines)


def compile_catalog(path: Path) -> bytes:
    """The .mo of a catalog (see the module)."""
    with path.open("rb") as f:
        catalog = read_po(f, abort_invalid=True)
    if catalog.charset.lower() != "utf-8":
        raise ValueError(f"{path}: not in UTF-8 ({catalog.charset})")
    if catalog.fuzzy:
        raise ValueError(f"{path}: its header is fuzzy")
    header = POT_CREATION_DATE.sub("", _header(path.read_text(encoding="utf-8"), path))
    entries = {b"": header.encode()}
    for message in catalog:
        if not message.id or message.fuzzy:
            continue
        # A message with a plural: its two msgids, and a translation per plural form.
        ids, strings = forms(message.id), forms(message.string)
        if not any(strings):
            continue
        if not all(strings):
            raise ValueError(f"{path}: partly translated: {ids[0]!r}")
        key = b"\0".join(text.encode() for text in ids)
        if message.context:
            key = message.context.encode() + b"\x04" + key
        entries[key] = b"\0".join(text.encode() for text in strings)
    return _mo(entries)


def forms(value: str | tuple[str, ...] | list[str] | None) -> list[str]:
    """A message's text, or its texts when it has a plural."""
    return [value] if isinstance(value, str) else list(value or ())


def _mo(entries: dict[bytes, bytes]) -> bytes:
    """GNU's MO format: the header, the table of the keys' lengths and offsets, the table of
    the values', then the keys and the values, each ended by a NUL byte."""
    keys = sorted(entries)
    count = len(keys)
    keys_start = HEADER_SIZE + 16 * count
    values_start = keys_start + sum(len(key) + 1 for key in keys)
    key_table, value_table = [], []
    key_offset, value_offset = keys_start, values_start
    for key in keys:
        key_table += [len(key), key_offset]
        value_table += [len(entries[key]), value_offset]
        key_offset += len(key) + 1
        value_offset += len(entries[key]) + 1
    header = struct.pack(
        "<7I",
        MAGIC,
        0,  # the format's revision
        count,
        HEADER_SIZE,  # the keys' table
        HEADER_SIZE + 8 * count,  # the values' table
        0,  # no hash table: its size,
        keys_start,  # and where it would be
    )
    tables = struct.pack(f"<{4 * count}I", *key_table, *value_table)
    strings = b"".join(key + b"\0" for key in keys)
    strings += b"".join(entries[key] + b"\0" for key in keys)
    return header + tables + strings


def compile_in_place(paths: list[Path]) -> None:
    for path in paths:
        path.with_suffix(".mo").write_bytes(compile_catalog(path))
        print(f"compiled {path}")


def compare(paths: list[Path]) -> bool:
    """Whether each catalog's .mo is GNU msgfmt's, byte for byte (`--no-hash`)."""
    msgfmt = shutil.which("msgfmt")
    if msgfmt is None:
        raise SystemExit("msgfmt (GNU gettext) is not installed")
    version = subprocess.run(
        [msgfmt, "--version"], check=True, capture_output=True, text=True
    ).stdout.splitlines()[0]
    print(version)
    same = True
    with tempfile.TemporaryDirectory() as directory:
        for path in paths:
            gnu = Path(directory) / "gnu.mo"
            subprocess.run(
                [msgfmt, "--check", "--no-hash", "-o", str(gnu), str(path)], check=True
            )
            ours = compile_catalog(path)
            equal = ours == gnu.read_bytes()
            same = same and equal
            print(f"{'same' if equal else 'DIFFERENT'}: {path} ({len(ours)} bytes)")
    return same


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("compile", "compare"):
        print(__doc__, file=sys.stderr)
        return 2
    paths = [Path(arg).resolve() for arg in argv[1:]] or catalogs()
    if not paths:
        raise SystemExit(f"no catalog in {LOCALE}")
    if argv[0] == "compile":
        compile_in_place(paths)
        return 0
    return 0 if compare(paths) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
