import pytest
from django.core.management import call_command


@pytest.mark.django_db
def test_migrations_match_the_models(settings):
    # pretix's test settings disable the migrations outside of GitHub Actions (tables are made
    # from the models): read them from their modules all the same.
    settings.MIGRATION_MODULES = {}
    call_command("makemigrations", "pretix_sattleme", "--check", "--dry-run")
