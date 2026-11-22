import re
import time
import tomllib
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.staticfiles import finders
from django.test import RequestFactory
from django.utils import translation
from django.utils.timezone import now
from django_scopes import scopes_disabled
from i18nfield.strings import LazyI18nString
from packaging.requirements import Requirement
from packaging.version import Version
from pretix.base.models import (
    CartPosition,
    Order,
    OrderFee,
    OrderPayment,
    OrderPosition,
)
from pretix.base.payment import PaymentException
from pretix.testutils.sessions import add_cart_session, get_cart_session_key
from sattleme_checkout import (
    ChannelNotFoundError,
    NetworkError,
    PaymentStatus,
    Rest,
    RevokedError,
)

from pretix_sattleme.apps import PluginApp
from pretix_sattleme.models import BitcoinInvoice
from pretix_sattleme.payment import Sattleme

from .conftest import KEY, OTHER_KEY, make_invoice

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = "/control/event/org/conf/settings/payment/sattleme"


def settings_form(**values):
    data = {
        "payment_sattleme__enabled": "on",
        "payment_sattleme_invoice_minutes": "15",
        "payment_sattleme__restrict_to_sales_channels": "web",
    }
    data.update({f"payment_sattleme_{k}": v for k, v in values.items()})
    return data


@pytest.mark.django_db
def test_checkout(client, fake_sdk, event, item):
    add_cart_session(client, event, {"email": "customer@example.org"})
    with scopes_disabled():
        CartPosition.objects.create(
            event=event,
            cart_id=get_cart_session_key(client, event),
            item=item,
            price=item.default_price,
            expires=now() + timedelta(minutes=10),
        )
    client.get("/org/conf/checkout/payment/", follow=True)
    client.post("/org/conf/checkout/questions/", {"email": "customer@example.org"})
    response = client.post(
        "/org/conf/checkout/payment/", {"payment": "sattleme"}, follow=True
    )
    assert "QR code to scan" in response.content.decode()
    assert fake_sdk.created == []  # Nothing is made before the order is confirmed.

    response = client.post("/org/conf/checkout/confirm/", follow=True)
    assert response.status_code == 200
    html = response.content.decode()
    assert "Scan this QR code with your Bitcoin wallet." in html
    assert len(fake_sdk.created) == 1

    with scopes_disabled():
        order = Order.objects.get()
        payment = order.payments.get()
        row = BitcoinInvoice.objects.get()
    assert response.redirect_chain[-1][0].endswith(
        f"/sattleme/{order.code}/{order.tagged_secret('plugins:pretix_sattleme')}/{payment.pk}/"
    )
    assert (
        row.payment == payment and payment.state == OrderPayment.PAYMENT_STATE_PENDING
    )

    # The order's page offers to go on with the payment.
    order_page = client.get(
        f"/org/conf/order/{order.code}/{order.secret}/"
    ).content.decode()
    assert "Pay with Bitcoin" in order_page

    fake_sdk.statuses = [PaymentStatus.paid_lightning("cd" * 32)]
    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=row.pk).update(checked=None)
    answer = client.get(response.redirect_chain[-1][0] + "status/").json()
    assert answer["phase"] == "paid"
    order.refresh_from_db()
    assert order.status == Order.STATUS_PAID


@pytest.mark.django_db
def test_not_offered_without_key(provider, event, rf):
    event.settings.delete("payment_sattleme_key")
    request = rf.get("/")
    request.session = {}
    assert not provider.is_allowed(request, total=10)


@pytest.mark.django_db
def test_offered_as_pretix_availability_settings_say(provider, event, rf):
    request = rf.get("/")
    request.session = SessionStore()
    request.event = event
    assert provider.is_allowed(request, total=Decimal("24.50"))
    # pretix's own settings of a payment method: here, the highest total it is offered for.
    event.settings.set("payment_sattleme__total_max", "20.00")
    assert not provider.is_allowed(request, total=Decimal("24.50"))
    assert provider.is_allowed(request, total=Decimal("19.50"))


def checkout_request(event):
    request = RequestFactory().get("/")
    request.session = SessionStore()
    request.event = event
    return request


@pytest.mark.django_db
def test_not_offered_in_a_currency_the_sdk_does_not_price(provider, event, order):
    """CHE, a fund: a currency of pretix, none of the SDK's table (currency_digits)."""
    event.currency = "CHE"
    event.save()
    assert not provider.is_allowed(checkout_request(event), total=Decimal("24.50"))
    with scopes_disabled():
        assert not provider.order_change_allowed(order)

    event.currency = "CHF"
    event.save()
    assert provider.is_allowed(checkout_request(event), total=Decimal("24.50"))
    with scopes_disabled():
        assert provider.order_change_allowed(order)


@pytest.mark.django_db
def test_offered_for_a_total_the_sdk_takes(provider, event):
    """RSD: pretix rounds its amounts to 2 places, the SDK prices it in whole dinars. The
    payment is the order's total and this method's fee."""
    event.currency = "RSD"
    event.save()
    request = checkout_request(event)
    assert provider.is_allowed(request, total=Decimal("1500.00"))
    assert not provider.is_allowed(request, total=Decimal("1500.50"))
    assert not provider.is_allowed(request, total=Decimal("0.00"))

    event.settings.set("payment_sattleme__fee_abs", "0.50")
    assert not provider.is_allowed(request, total=Decimal("1500.00"))
    assert provider.is_allowed(request, total=Decimal("1499.50"))

    # A currency of 3 decimals, which pretix rounds to 2: every amount.
    event.currency = "KWD"
    event.save()
    event.settings.delete("payment_sattleme__fee_abs")
    assert provider.is_allowed(request, total=Decimal("12.34"))


@pytest.mark.django_db
def test_change_offered_for_what_the_order_owes_then(provider, event, item):
    """A customer changing their order's payment method: the payment comes to what the order
    owes, its open payment fee (another method's) replaced by this method's."""
    event.currency = "RSD"
    event.save()
    with scopes_disabled():
        order = Order.objects.create(
            code="RSD12",
            event=event,
            email="customer@example.org",
            status=Order.STATUS_PENDING,
            datetime=now(),
            expires=now() + timedelta(days=10),
            total=Decimal("1500.50"),
            locale="en",
            sales_channel=event.organizer.sales_channels.get(identifier="web"),
        )
        position = OrderPosition.objects.create(
            order=order, item=item, price=Decimal("1500.50")
        )
        assert not provider.order_change_allowed(order)

        # 1500.00 and another method's fee of 0.50, which this method's replaces.
        position.price = Decimal("1500.00")
        position.save()
        fee = order.fees.create(
            fee_type=OrderFee.FEE_TYPE_PAYMENT,
            value=Decimal("0.50"),
            internal_type="manual",
        )
        order.payments.create(
            provider="manual",
            state=OrderPayment.PAYMENT_STATE_CREATED,
            amount=Decimal("1500.50"),
            fee=fee,
        )
        assert provider.order_change_allowed(order)
        event.settings.set("payment_sattleme__fee_abs", "0.25")
        assert not provider.order_change_allowed(order)
        event.settings.set("payment_sattleme__fee_abs", "1.00")
        assert provider.order_change_allowed(order)

        # A fee that a confirmed payment holds is no longer open: the payment comes to what
        # the order owes, this method's fee added.
        order.payments.update(
            state=OrderPayment.PAYMENT_STATE_CONFIRMED, amount=Decimal("500.50")
        )
        event.settings.delete("payment_sattleme__fee_abs")
        assert order.pending_sum == Decimal("1000.00")
        assert provider.order_change_allowed(order)
        event.settings.set("payment_sattleme__fee_abs", "0.50")
        assert not provider.order_change_allowed(order)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "currency, price, offered",
    [("CHF", "24.50", True), ("CHE", "24.50", False), ("RSD", "1500.50", False)],
)
def test_checkout_offers_bitcoin_for_a_price_the_sdk_takes(
    client, event, item, currency, price, offered
):
    event.currency = currency
    event.save()
    # Another method, so that the page lists the methods to choose from.
    event.plugins = "pretix_sattleme,pretix.plugins.manualpayment"
    event.save()
    event.settings.set("payment_manual__enabled", True)
    item.default_price = Decimal(price)
    item.save()
    add_cart_session(client, event, {"email": "customer@example.org"})
    with scopes_disabled():
        CartPosition.objects.create(
            event=event,
            cart_id=get_cart_session_key(client, event),
            item=item,
            price=item.default_price,
            expires=now() + timedelta(minutes=10),
        )
    client.get("/org/conf/checkout/payment/", follow=True)
    client.post("/org/conf/checkout/questions/", {"email": "customer@example.org"})
    html = client.get("/org/conf/checkout/payment/", follow=True).content.decode()
    assert 'value="manual"' in html
    assert ('value="sattleme"' in html) == offered


@pytest.mark.django_db
def test_settings_page(client, admin, event):
    client.force_login(admin)
    html = client.get(SETTINGS).content.decode()
    assert "Integration key" in html
    assert 'name="payment_sattleme_public_name_0"' in html
    assert 'name="payment_sattleme_checkout_description_0"' in html
    # A key once saved is never shown again.
    assert KEY not in html
    # 15 minutes to pay, unless the organizer chooses otherwise (up to an hour).
    assert 'name="payment_sattleme_invoice_minutes" value="15"' in html


@pytest.mark.django_db
@pytest.mark.parametrize(
    "currency, said",
    [
        ("CHF", None),
        ("KWD", None),
        ("CHE", "This event's currency, CHE, is not one sattle.me prices orders in"),
        ("RSD", "sattle.me prices orders in RSD in whole units"),
        ("ISK", "sattle.me prices orders in ISK in whole units"),
    ],
)
def test_settings_page_says_when_the_currency_cannot_be_used(
    client, admin, event, currency, said
):
    event.currency = currency
    event.save()
    client.force_login(admin)
    html = " ".join(client.get(SETTINGS).content.decode().split())
    html = html.replace("&#x27;", "'")
    assert "Integration key" in html
    unusable = (
        "is not one sattle.me prices orders in: the payment method is not offered"
    )
    whole = (
        "in whole units: the payment method is offered to customers only for amounts "
        "without decimals"
    )
    assert (unusable in html) == (currency == "CHE")
    assert (whole in html) == (currency in ("RSD", "ISK"))
    assert said is None or said in html


@pytest.mark.django_db
def test_settings_verify_a_new_key(client, admin, event, fake_sdk):
    client.force_login(admin)
    event.settings.delete("payment_sattleme_key")
    response = client.post(SETTINGS, settings_form(key=KEY), follow=True)
    assert "Your changes have been saved." in response.content.decode()
    event.settings.flush()
    assert event.settings.get("payment_sattleme_key") == KEY
    assert len(fake_sdk.options) == 1


def key_input(html):
    """The value of the key's input in a settings page ("" without one)."""
    [tag] = re.findall(r'<input[^>]*name="payment_sattleme_key"[^>]*>', html)
    value = re.search(r'value="([^"]*)"', tag)
    return value.group(1) if value else ""


@pytest.mark.django_db
def test_settings_never_send_a_key_back(client, admin, event, fake_sdk):
    """A key refused is not shown again either: the form shown again holds the key saved,
    redacted, or nothing."""
    client.force_login(admin)
    fake_sdk.merchant = RevokedError("removed")
    html = client.post(SETTINGS, settings_form(key=OTHER_KEY)).content.decode()
    assert "This integration was removed" in html
    assert OTHER_KEY not in html and KEY not in html and key_input(html) == "*****"

    event.settings.delete("payment_sattleme_key")
    html = client.post(SETTINGS, settings_form(key=OTHER_KEY)).content.decode()
    assert "This integration was removed" in html
    assert OTHER_KEY not in html and key_input(html) == ""
    # Nor a key the SDK refuses as such.
    html = client.post(SETTINGS, settings_form(key=KEY[:-4])).content.decode()
    assert "This is not the secret key" in html and KEY[:-4] not in html


@pytest.mark.django_db
def test_the_key_never_reaches_pretix_log(client, admin, event, fake_sdk):
    """pretix logs the new values of a payment method's settings: the key's is hidden."""
    from pretix_sattleme.signals import REDACTED

    client.force_login(admin)
    response = client.post(
        SETTINGS, settings_form(key=OTHER_KEY, invoice_minutes="20"), follow=True
    )
    assert "Your changes have been saved." in response.content.decode()
    event.settings.flush()
    assert event.settings.get("payment_sattleme_key") == OTHER_KEY
    with scopes_disabled():
        entries = list(event.logentry_set.all())
    assert entries and all(OTHER_KEY not in e.data for e in entries)
    [saved] = [e for e in entries if e.action_type == "pretix.event.payment.provider"]
    values = saved.parsed_data["new_values"]
    assert values["payment_sattleme_key"] == REDACTED
    assert values["payment_sattleme_invoice_minutes"] == 20
    assert (
        str(saved.display()) == "The settings of a payment provider have been changed."
    )


@pytest.mark.parametrize(
    "key, merchant, message",
    [
        (
            "sattleme_ck_nope",
            None,
            "This is not the secret key of a sattle.me integration",
        ),
        (KEY, RevokedError("removed"), "This integration was removed"),
        (KEY, ChannelNotFoundError("none", newer_format=True), "needs a newer version"),
        (KEY, ChannelNotFoundError("none", newer_format=False), "could not be found"),
        (KEY, NetworkError("no relay"), "could not be verified"),
    ],
)
@pytest.mark.django_db
def test_settings_refuse_a_bad_key(
    client, admin, event, fake_sdk, key, merchant, message
):
    client.force_login(admin)
    event.settings.delete("payment_sattleme_key")
    if merchant:
        fake_sdk.merchant = merchant
    response = client.post(SETTINGS, settings_form(key=key))
    assert message in response.content.decode()
    event.settings.flush()
    assert not event.settings.get("payment_sattleme_key")


@pytest.mark.django_db
def test_settings_keep_the_key_unverified_when_unchanged(
    client, admin, event, fake_sdk
):
    client.force_login(admin)
    response = client.post(
        SETTINGS, settings_form(key="*****", invoice_minutes="30"), follow=True
    )
    assert "Your changes have been saved." in response.content.decode()
    event.settings.flush()
    assert event.settings.get("payment_sattleme_key") == KEY
    assert event.settings.get("payment_sattleme_invoice_minutes", as_type=int) == 30
    assert fake_sdk.options == []


@pytest.mark.django_db
def test_abort_allowed_only_once_the_invoice_ended_unpaid(
    provider, invoice_row, payment
):
    """Ended unpaid: known only from its final status, `expired` (received 0) from a check
    after its end. Before, its time being over is not enough (test_one_payable_invoice.py).
    """

    def allowed():
        return provider._payment_abort_pending_allowed(
            OrderPayment.objects.get(pk=payment.pk)
        )

    with scopes_disabled():
        # Payable: the customer could pay it after switching.
        assert not allowed()
        # Its time and grace period over, never checked since: it may have been paid.
        invoice = make_invoice(created=int(time.time()) - 1100)
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(data=invoice.to_json())
        assert not allowed()
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(
            status=PaymentStatus.open(1, 30_000, Rest(19_000, None, None)).to_json()
        )
        assert not allowed()
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(
            status=PaymentStatus.unconfirmed(49_000, 0, ()).to_json()
        )
        assert not allowed()
        # A check after its end found it expired, nothing received: over, for good (its
        # payment failed, as that check does).
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(
            status=PaymentStatus.expired(0).to_json(), final=True
        )
        OrderPayment.objects.filter(pk=payment.pk).update(state="failed")
        assert allowed()


@pytest.mark.django_db
def test_never_refunds_on_its_own(provider, invoice_row, payment):
    """A Lightning payment can only be refunded to an invoice of the customer's: the organizer
    refunds by hand, outside pretix's refunds through the provider."""
    with scopes_disabled():
        assert not provider.payment_refund_supported(payment)
        assert not provider.payment_partial_refund_supported(payment)


@pytest.mark.django_db
def test_cancel_a_payment_only_once_its_invoice_is_final(
    provider, invoice_row, payment
):
    """pretix cancels the open payment of an order canceled, changed or paid otherwise: not
    while its invoice may be paid, which pretix logs and shows (PaymentException)."""
    with scopes_disabled():
        with pytest.raises(PaymentException, match="may still be paid"):
            provider.cancel_payment(payment)
        payment.refresh_from_db()
        assert payment.state == OrderPayment.PAYMENT_STATE_PENDING

        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(
            status=PaymentStatus.expired(0).to_json(), final=True
        )
        provider.cancel_payment(payment)
        payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CANCELED


@pytest.mark.django_db
def test_cancel_a_payment_whose_invoice_was_not_made(provider, payment):
    with scopes_disabled():
        BitcoinInvoice.objects.create(payment=payment, ref="pretix-ABC12-1-none")
        provider.cancel_payment(payment)
        payment.refresh_from_db()
    assert payment.state == OrderPayment.PAYMENT_STATE_CANCELED


@pytest.mark.django_db
def test_renders(provider, invoice_row, payment, order, rf):
    request = rf.get("/")
    with scopes_disabled():
        payment = OrderPayment.objects.get(pk=payment.pk)
        pending = provider.payment_pending_render(request, payment)
        assert "Pay with Bitcoin" in pending and "/sattleme/" in pending

        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(
            status=PaymentStatus.paid_onchain(49_000, ("ef" * 32,)).to_json()
        )
        payment = OrderPayment.objects.get(pk=payment.pk)
        control = provider.payment_control_render(request, payment)
        assert invoice_row.ref in control
        assert "https://mempool.space/tx/" + "ef" * 32 in control
        assert "1 BTC = 50000 CHF" in control

        details = provider.api_payment_details(payment)
        assert (
            details["ref"] == invoice_row.ref and details["status"]["state"] == "paid"
        )
        assert provider.matching_id(payment) == invoice_row.ref

        mail = provider.order_pending_mail_render(order, payment)
        assert "QR code" in mail

        other = order.payments.create(provider="sattleme", state="created", amount=1)
        assert provider.payment_pending_render(request, other) == ""
        assert provider.payment_control_render(request, other) == ""
        assert provider.api_payment_details(other) == {}


@pytest.mark.django_db
def test_a_mixed_payment_shows_its_transactions_and_proofs(
    provider, invoice_row, payment, rf
):
    status = PaymentStatus.paid_mixed(49_000, ("ef" * 32,), ("cd" * 32,))
    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(status=status.to_json())
        payment = OrderPayment.objects.get(pk=payment.pk)
        html = provider.payment_control_render(rf.get("/"), payment)
    assert "paid" in html and "(on-chain and in Lightning)" in html
    assert 'href="https://mempool.space/tx/' + "ef" * 32 + '"' in html
    assert "Proof of payment" in html and "cd" * 32 in html


@pytest.mark.django_db
def test_errors_are_shown_in_plain_words(provider, invoice_row, payment, order, rf):
    request = rf.get("/")
    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(error="apply", errors=2)
        payment = OrderPayment.objects.get(pk=payment.pk)
        html = provider.payment_control_render(request, payment)
        assert "failed: pretix could not apply what the check found" in html

        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(
            error="abandoned", final=True
        )
        payment = OrderPayment.objects.get(pk=payment.pk)
        html = provider.payment_control_render(request, payment)
        assert "its checks stopped: no final status after 90 days" in html

        # A record whose invoice could not be made: its reference, and why.
        other = order.payments.create(
            provider="sattleme", state="created", amount=order.total
        )
        BitcoinInvoice.objects.create(payment=other, ref="pretix-r", error="revoked")
        other = OrderPayment.objects.get(pk=other.pk)
        html = provider.payment_control_render(request, other)
        assert "pretix-r" in html and "no invoice made" in html
        assert "failed: the integration was removed in the sattle.me app" in html
        assert provider.api_payment_details(other) == {}
        assert provider.matching_id(other) is None

        order.log_action(
            "pretix_sattleme.invoice.failed", data={"local_id": 2, "error": "network"}
        )
        [entry] = order.all_logentries().filter(
            action_type="pretix_sattleme.invoice.failed"
        )
    assert str(entry.display()).startswith(
        "No Bitcoin invoice could be created (the merchant's relays, its Lightning "
        "address,"
    )


def test_identifier():
    assert Sattleme.identifier == "sattleme"


def test_picture_is_shipped():
    # pretix's lists of plugins show it, 32 px high: a static file of this plugin, an SVG of no
    # script, and the brand's square icon (its logo needs 80 px of width).
    picture = PluginApp.PretixPluginMeta.picture
    path = finders.find(picture)
    assert picture == "pretix_sattleme/sattleme-icon.svg" and path
    with open(path, encoding="utf-8") as f:
        svg = f.read()
    assert svg.startswith("<svg ") and not re.search(r"<script|href|\son\w+=", svg)
    assert 'viewBox="0 0 512 512"' in svg and 'aria-label="sattle.me"' in svg


def test_compatibility_is_the_oldest_pretix_tested():
    # The lockfile's groups (pyproject.toml): the last three feature releases of pretix, each at
    # one release of its own. pretix refuses to start with a plugin whose compatibility excludes
    # it, so a bound above the oldest fails that group's tests; one below would let a pretix no
    # test runs on install the plugin, as when the oldest group is dropped and not the bound.
    groups = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "dependency-groups"
    ]
    tested = {}
    for name, requirements in groups.items():
        if name.startswith("pretix-"):
            (text,) = requirements
            requirement = Requirement(text)
            (specifier,) = requirement.specifier
            assert requirement.name == "pretix" and specifier.operator == "=="
            version = Version(specifier.version)
            assert name == f"pretix-{version.major}-{version.minor}"
            tested[name] = version
    assert len(tested) == 3
    oldest = min(tested.values())
    compatibility = PluginApp.PretixPluginMeta.compatibility
    assert compatibility == f"pretix>={oldest.major}.{oldest.minor}.0"


@pytest.mark.django_db
def test_name_and_description_at_checkout(provider, event, rf):
    request = rf.get("/")
    assert provider.public_name == "Bitcoin"
    assert "Pay with your Bitcoin wallet" in provider.payment_form_render(request)

    event.settings.set(
        "payment_sattleme_public_name",
        LazyI18nString({"en": "Bitcoin or Lightning", "de": "Bitcoin oder Lightning"}),
    )
    event.settings.set(
        "payment_sattleme_checkout_description",
        LazyI18nString(
            {
                "en": "**Fast**, with any wallet. <script>alert(1)</script>",
                "de": "**Schnell**, mit jedem Wallet.",
            }
        ),
    )
    assert provider.public_name == "Bitcoin or Lightning"
    html = provider.payment_form_render(request)
    assert "<strong>Fast</strong>, with any wallet." in html
    assert "<script>" not in html and "Pay with your Bitcoin wallet" not in html
    with translation.override("de"):
        assert provider.public_name == "Bitcoin oder Lightning"
        assert "<strong>Schnell</strong>" in provider.payment_form_render(request)


@pytest.mark.django_db
def test_test_mode_message(provider):
    assert "no test mode" in str(provider.test_mode_message)
