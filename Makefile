all: localecompile
LNGS:=`find pretix_sattleme/locale/ -mindepth 1 -maxdepth 1 -type d -printf "-l %f "`

# Each catalog's .mo next to it, as the build compiles them (scripts/translations.py).
localecompile:
	python scripts/translations.py compile

# The catalogs made again from the sources (GNU gettext's xgettext and msgmerge, through Django).
localegen:
	django-admin makemessages --add-location file --keep-pot -i build -i dist -i "*egg*" -i tests -i sdk -i scripts -i e2e $(LNGS)

.PHONY: all localecompile localegen
