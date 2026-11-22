import inspect
import time
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import translation
from django.utils.timezone import now
from django_scopes import scopes_disabled
from pretix.base.models import (
    Event,
    Item,
    Order,
    OrderPayment,
    OrderPosition,
    Organizer,
    Quota,
    Team,
    User,
)
from sattleme_checkout import (
    Client,
    DeclaredInvoice,
    Fiat,
    Invoice,
    Merchant,
    PaymentStatus,
    Price,
    RateInfo,
)

from pretix_sattleme.models import BitcoinInvoice

# An integration key of the specification's test vectors (integration-keys.json, "the app's
# vector"): valid, of no merchant (its relay is relay.example). The tests never reach the network.
KEY = (
    "sattleme_ck_qEVejHuCZpF4XMmBSw5p7iZYxCtm20XxzT1fnJ9VB8o."
    "uPTAz1d8bbQaw2Jx22Wwrga2vI2F8allY5NrPC3Aogk.cmVsYXkuZXhhbXBsZQ"
)
# Another one ("several relays, a port and a path").
OTHER_KEY = (
    "sattleme_ck_qEVejHuCZpF4XMmBSw5p7iZYxCtm20XxzT1fnJ9VB8o."
    "uPTAz1d8bbQaw2Jx22Wwrga2vI2F8allY5NrPC3Aogk."
    "cmVsYXkuZXhhbXBsZSxub3MubG9sOjQ0NDMscmVsYXkuZXhhbXBsZS5jaC9ub3N0cg"
)
ADDRESS = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    # As in pretix's own tests: fixtures run without django-scopes.
    if inspect.isgeneratorfunction(fixturedef.func):
        yield
    else:
        with scopes_disabled():
            yield


def pytest_runtest_setup(item):
    # Tests of PostgreSQL's row locks (marker `postgresql`): failed on another database, never
    # skipped. On SQLite, deselect them (-m "not postgresql"); run them on PostgreSQL.
    if item.get_closest_marker("postgresql"):
        from django.db import connection

        if connection.vendor != "postgresql":
            pytest.fail(
                "This test needs PostgreSQL (its row locks): run it on PostgreSQL, or "
                'deselect it on SQLite with -m "not postgresql".',
                pytrace=False,
            )


@pytest.fixture(autouse=True)
def reset_locale():
    translation.activate("en")


@pytest.fixture(autouse=True)
def unhurried_time_budgets(monkeypatch):
    """The plugin's budgets of wall-clock time (the wait for an order's lock, the checks a request
    runs inline) out of reach: they measure the machine, which a loaded CI runner makes many times
    slower, and no test but theirs is about them. Their own tests set them, with a fake clock.
    """
    from pretix_sattleme import checks, invoices

    monkeypatch.setattr(invoices, "LOCK_WAIT", 3600)
    monkeypatch.setattr(checks, "INLINE_BUDGET", 3600 + checks.WEB_TIMEOUT)


@pytest.fixture(autouse=True)
def reset_hierarkey_cache_state():
    from hierarkey.proxy import dirty_cache_keys

    dirty_cache_keys.set(set())


def make_invoice(
    ref="pretix-ABC12-1-test",
    sats=49_000,
    lightning=True,
    onchain=False,
    duration=900,
    created=None,
    fiat=Price("24.50", "CHF"),
):
    """An Invoice as the SDK returns it (its JSON is what the plugin stores), of `fiat`."""
    created = int(time.time()) if created is None else created
    minor = int(Decimal(fiat.amount) * 100)
    bolt11 = "lnbc490u1p5" + "q" * 300 if lightning else None
    address = ADDRESS if onchain else None
    if address:
        uri = f"bitcoin:{address}?amount=0.00049" + (
            f"&lightning={bolt11}" if bolt11 else ""
        )
    else:
        uri = f"lightning:{bolt11}"
    return Invoice(
        ref=ref,
        bolt11=bolt11,
        payment_hash="ab" * 32 if lightning else None,
        verify_url=(
            "https://breez.tips/lnurlp/verify/" + "ab" * 32 if lightning else None
        ),
        onchain_address=address,
        payment_uri=uri,
        sats=sats,
        fiat=Fiat(
            amount=f"{minor // 100}.{minor % 100:02d}",
            currency=fiat.currency,
            minor=minor,
            digits=2,
        ),
        rate=RateInfo(
            source="coingecko",
            currency=fiat.currency,
            price="50000",
            fetched_at=created,
            time=created,
        ),
        note="Conference · ABC12",
        created_at=created,
        expires_at=created + duration,
        checkout_url=None,
        redirect_url=None,
    )


def declared(invoice: Invoice) -> DeclaredInvoice:
    """The invoice as the merchant's registry holds it, as the SDK's errors carry it."""
    return DeclaredInvoice(
        ref=invoice.ref,
        bolt11=invoice.bolt11,
        payment_hash=invoice.payment_hash,
        verify_url=invoice.verify_url,
        onchain_address=invoice.onchain_address,
        payment_uri=invoice.payment_uri,
        sats=invoice.sats,
        fiat=invoice.fiat,
        note=invoice.note,
        created_at=invoice.created_at,
        expires_at=invoice.expires_at,
        declared_at=invoice.created_at + 1,
    )


class FakeSDK:
    """Stands for the SDK's network calls. Each client is first made by the real SDK, which
    checks the key and the options as in production, then answers from here."""

    def __init__(self):
        self.invoice = (
            None  # Invoice or exception for create_invoice; None: made from the order
        )
        self.statuses = []  # PaymentStatus or exception, one per get_status
        self.merchant = Merchant(
            business_name="Café", min_sats=1, max_sats=10_000_000, onchain_addresses=10
        )
        self.created = []  # keyword arguments of each create_invoice
        self.checked = []  # (invoice, timeout) of each get_status
        self.options = []  # options of each client
        self.delay = 0  # seconds each create_invoice takes

    def client(self, key, **options):
        Client(key, **options)
        self.options.append(options)
        return FakeClient(self)


class FakeClient:
    def __init__(self, sdk):
        self.sdk = sdk

    def create_invoice(self, **kwargs):
        self.sdk.created.append(kwargs)
        time.sleep(self.sdk.delay)
        result = self.sdk.invoice
        if isinstance(result, BaseException):
            raise result
        return result or make_invoice(
            ref=kwargs["ref"], duration=kwargs["duration_seconds"], fiat=kwargs["fiat"]
        )

    def get_status(self, invoice, *, timeout=None):
        self.sdk.checked.append((invoice, timeout))
        result = (
            self.sdk.statuses.pop(0)
            if self.sdk.statuses
            else PaymentStatus.open(0, 0, None)
        )
        if isinstance(result, BaseException):
            raise result
        return result

    def merchant(self, *, timeout=None):
        if isinstance(self.sdk.merchant, BaseException):
            raise self.sdk.merchant
        return self.sdk.merchant


def _no_network(*args, **kwargs):
    raise AssertionError("A test reached the SDK without the fake_sdk fixture")


# The plugin makes every client in sdk.new_client: the one seam replaced here.
@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr("pretix_sattleme.sdk.Client", _no_network)


@pytest.fixture
def fake_sdk(monkeypatch):
    fake = FakeSDK()
    monkeypatch.setattr("pretix_sattleme.sdk.Client", fake.client)
    return fake


@pytest.fixture
def organizer():
    return Organizer.objects.create(name="Org", slug="org")


@pytest.fixture
def event(organizer):
    event = Event.objects.create(
        organizer=organizer,
        name="Conference",
        slug="conf",
        currency="CHF",
        date_from=now() + timedelta(days=30),
        live=True,
        plugins="pretix_sattleme",
    )
    event.settings.set("payment_sattleme__enabled", True)
    event.settings.set("payment_sattleme_key", KEY)
    event.settings.set("attendee_names_asked", False)
    return event


@pytest.fixture
def item(event):
    quota = Quota.objects.create(event=event, name="Tickets", size=10)
    item = Item.objects.create(
        event=event, name="Ticket", default_price=Decimal("24.50")
    )
    quota.items.add(item)
    return item


@pytest.fixture
def order(event, item):
    order = Order.objects.create(
        code="ABC12",
        event=event,
        email="customer@example.org",
        status=Order.STATUS_PENDING,
        datetime=now(),
        expires=now() + timedelta(days=10),
        total=Decimal("24.50"),
        locale="en",
        sales_channel=event.organizer.sales_channels.get(identifier="web"),
    )
    OrderPosition.objects.create(order=order, item=item, price=Decimal("24.50"))
    return order


@pytest.fixture
def payment(order):
    return order.payments.create(
        provider="sattleme",
        state=OrderPayment.PAYMENT_STATE_CREATED,
        amount=order.total,
    )


@pytest.fixture
def provider(event):
    from pretix_sattleme.payment import Sattleme

    return Sattleme(event)


@pytest.fixture
def invoice_row(payment):
    """A payment with its invoice, as execute_payment leaves them."""
    invoice = make_invoice()
    row = BitcoinInvoice.objects.create(
        payment=payment,
        ref=invoice.ref,
        data=invoice.to_json(),
        next_check=now(),
    )
    payment.state = OrderPayment.PAYMENT_STATE_PENDING
    payment.save(update_fields=["state"])
    return row


@pytest.fixture
def admin(event):
    user = User.objects.create_user("admin@example.org", "admin")
    team = Team.objects.create(organizer=event.organizer, all_event_permissions=True)
    team.members.add(user)
    team.limit_events.add(event)
    return user
