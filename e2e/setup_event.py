"""What the end-to-end test starts from, made in pretix's shell (`python -m pretix shell <
e2e/setup_event.py`): the test's user, an organizer whose team holds that user with every
permission, and an event on sale in English and German (one product at CHF 24.50) without the
plugin, which the test enables through pretix's backend."""

from datetime import timedelta
from decimal import Decimal

from django.utils.timezone import now
from django_scopes import scopes_disabled
from pretix.base.models import Event, Item, Organizer, Quota, Team, User

EMAIL = "e2e@example.org"
# Of a throwaway pretix, made for one run of the test.
PASSWORD = "e2e-test-password"

with scopes_disabled():
    user = User.objects.create_user(EMAIL, PASSWORD)
    organizer = Organizer.objects.create(name="sattle.me e2e", slug="e2e")
    team = Team.objects.create(
        organizer=organizer,
        name="Administrators",
        all_events=True,
        all_event_permissions=True,
        all_organizer_permissions=True,
    )
    team.members.add(user)
    event = Event.objects.create(
        organizer=organizer,
        name="Conference",
        slug="conf",
        currency="CHF",
        date_from=now() + timedelta(days=30),
        live=True,
    )
    event.settings.set("attendee_names_asked", False)
    event.settings.set("locales", ["en", "de"])
    quota = Quota.objects.create(event=event, name="Tickets", size=10)
    item = Item.objects.create(
        event=event, name="Ticket", default_price=Decimal("24.50")
    )
    quota.items.add(item)
    print(f"e2e: event {organizer.slug}/{event.slug}, product {item.pk}")
