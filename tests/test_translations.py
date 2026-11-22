"""The plugin's translations: every message translated, and the catalogs compiled as GNU msgfmt
compiles them (scripts/translations.py, which the build runs)."""

import base64
import gettext
import io
import re

import pytest
import translations
from babel.messages.pofile import read_po
from translations import forms

CATALOGS = translations.catalogs()
# Placeholders: Python's by name ("%(error)s") or by position ("%s"), and str.format's.
PLACEHOLDER = re.compile(r"%\([a-z_]+\)[sd]|%[sd]|\{[a-z_]*\}")

# A catalog of every kind of entry, and what GNU gettext 0.21's `msgfmt --no-hash` wrote of it.
REFERENCE = r"""msgid ""
msgstr ""
"Project-Id-Version: test\n"
"POT-Creation-Date: 2026-10-24 12:37+0000\n"
"Language: fr\n"
"MIME-Version: 1.0\n"
"Content-Type: text/plain; charset=UTF-8\n"
"Content-Transfer-Encoding: 8bit\n"
"Plural-Forms: nplurals=2; plural=(n > 1);\n"

msgid "Paid"
msgstr "Payé"

msgctxt "button"
msgid "Open"
msgstr "Ouvrir"

msgid "{n} ticket"
msgid_plural "{n} tickets"
msgstr[0] "{n} billet"
msgstr[1] "{n} billets"

msgid "a \"quoted\"\tword\n"
msgstr "un mot « cité »\t\n"

#, fuzzy
msgid "Fuzzy"
msgstr "Flou"

msgid "Untranslated"
msgstr ""

#~ msgid "Obsolete"
#~ msgstr "Obsolète"
"""
REFERENCE_MO = base64.b64decode(
    "3hIElQAAAAAFAAAAHAAAAEQAAAAAAAAAbAAAAAAAAABsAAAABAAAAG0AAAAQAAAAcgAAAAsAAACD"
    "AAAAFgAAAI8AAACqAAAApgAAAAUAAABRAQAAFAAAAFcBAAAGAAAAbAEAABYAAABzAQAAAFBhaWQA"
    "YSAicXVvdGVkIgl3b3JkCgBidXR0b24ET3BlbgB7bn0gdGlja2V0AHtufSB0aWNrZXRzAFByb2pl"
    "Y3QtSWQtVmVyc2lvbjogdGVzdApMYW5ndWFnZTogZnIKTUlNRS1WZXJzaW9uOiAxLjAKQ29udGVu"
    "dC1UeXBlOiB0ZXh0L3BsYWluOyBjaGFyc2V0PVVURi04CkNvbnRlbnQtVHJhbnNmZXItRW5jb2Rp"
    "bmc6IDhiaXQKUGx1cmFsLUZvcm1zOiBucGx1cmFscz0yOyBwbHVyYWw9KG4gPiAxKTsKAFBhecOp"
    "AHVuIG1vdCDCqyBjaXTDqSDCuwkKAE91dnJpcgB7bn0gYmlsbGV0AHtufSBiaWxsZXRzAA=="
)


def language(path):
    return path.parent.parent.name


def test_every_language_has_its_catalog():
    assert [language(p) for p in CATALOGS] == ["de", "de_Informal", "fr", "it"]


def placeholders(text):
    return sorted(PLACEHOLDER.findall(text))


@pytest.mark.parametrize("path", CATALOGS, ids=language)
def test_every_message_is_translated_with_its_placeholders(path):
    with path.open("rb") as f:
        catalog = read_po(f, abort_invalid=True)
    messages = [m for m in catalog if m.id]
    assert len(messages) > 100 and not catalog.obsolete
    for message in messages:
        ids, strings = forms(message.id), forms(message.string)
        assert all(strings) and not message.fuzzy, ids[0]
        for text in strings:
            assert placeholders(text) == placeholders(ids[-1]), ids[0]


@pytest.mark.parametrize("path", CATALOGS, ids=language)
def test_compiled_as_pretix_reads_them(path):
    with path.open("rb") as f:
        catalog = read_po(f, abort_invalid=True)
    compiled = gettext.GNUTranslations(io.BytesIO(translations.compile_catalog(path)))
    for message in catalog:
        ids, strings = forms(message.id), forms(message.string)
        if len(ids) == 1 and ids[0]:
            assert compiled.gettext(ids[0]) == strings[0]
        elif len(ids) == 2:
            # 1 and 2: the plural forms 0 and 1 in each of these languages.
            assert compiled.ngettext(ids[0], ids[1], 1) == strings[0]
            assert compiled.ngettext(ids[0], ids[1], 2) == strings[1]
    info = compiled.info()
    assert info["language"] == language(path)
    assert info["content-type"] == "text/plain; charset=UTF-8"
    assert info["plural-forms"].startswith("nplurals=2;")
    assert "pot-creation-date" not in info


def test_compiled_as_gnu_msgfmt_compiles(tmp_path):
    path = tmp_path / "django.po"
    path.write_text(REFERENCE, encoding="utf-8")
    compiled = translations.compile_catalog(path)
    assert compiled == REFERENCE_MO

    read = gettext.GNUTranslations(io.BytesIO(compiled))
    assert read.gettext("Paid") == "Payé"
    assert read.pgettext("button", "Open") == "Ouvrir"
    assert read.gettext("Open") == "Open"
    assert read.ngettext("{n} ticket", "{n} tickets", 1) == "{n} billet"
    assert read.ngettext("{n} ticket", "{n} tickets", 2) == "{n} billets"
    assert read.gettext('a "quoted"\tword\n') == "un mot « cité »\t\n"
    # Neither a fuzzy translation nor an obsolete one, nor what has none.
    for text in ("Fuzzy", "Untranslated", "Obsolete"):
        assert read.gettext(text) == text


@pytest.mark.parametrize(
    "change, error",
    [
        (('msgstr[1] "{n} billets"', 'msgstr[1] ""'), "partly translated"),
        (('msgid ""\nmsgstr ""\n', '#, fuzzy\nmsgid ""\nmsgstr ""\n'), "fuzzy"),
        (("charset=UTF-8", "charset=ISO-8859-1"), "not in UTF-8"),
    ],
)
def test_catalogs_it_refuses(tmp_path, change, error):
    path = tmp_path / "django.po"
    text = REFERENCE.replace(*change)
    assert text != REFERENCE
    path.write_bytes(text.encode("latin-1" if "ISO-8859-1" in text else "utf-8"))
    with pytest.raises(ValueError, match=error):
        translations.compile_catalog(path)
