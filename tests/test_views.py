import time
from datetime import timedelta

import pytest
from django.utils.timezone import now
from django_scopes import scopes_disabled
from pretix.base.models import OrderPayment
from sattleme_checkout import PaymentStatus, Rest

from pretix_sattleme import links
from pretix_sattleme.models import BitcoinInvoice

from .conftest import make_invoice

TXID = "ef" * 32


def url(payment, suffix=""):
    with scopes_disabled():
        return links.pay_url(payment) + suffix


def set_status(row, status):
    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=row.pk).update(status=status.to_json())


@pytest.mark.django_db
def test_page_needs_the_tagged_secret(client, invoice_row, payment, order):
    good = url(payment)
    assert client.get(good).status_code == 200

    with scopes_disabled():
        tag = order.tagged_secret(links.SECRET_TAG)
        other = order.tagged_secret("plugins:paypal2:pay")
    assert client.get(good.replace(tag, other)).status_code == 404
    assert client.get(good.replace(tag, "0" * 64)).status_code == 404
    # The order's own secret does not open it either.
    assert (
        client.get(good.replace(tag, (order.secret * 4)[:64].lower())).status_code
        == 404
    )


@pytest.mark.parametrize("suffix", ["status/", "?check=1"])
@pytest.mark.django_db
def test_status_and_check_need_the_tagged_secret(
    client, fake_sdk, invoice_row, payment, order, suffix
):
    """The page's status endpoint and its check link are authorized as the page is: without
    the order's secret for this plugin, nothing is checked."""
    good = url(payment, suffix)
    with scopes_disabled():
        tag = order.tagged_secret(links.SECRET_TAG)
        other = order.tagged_secret("plugins:paypal2:pay")
    assert client.get(good.replace(tag, "0" * 64)).status_code == 404
    assert client.get(good.replace(tag, other)).status_code == 404
    assert fake_sdk.checked == []
    assert client.get(good).status_code in (200, 302)
    assert len(fake_sdk.checked) == 1


@pytest.mark.django_db
def test_page_needs_a_payment_of_this_provider_and_order(
    client, invoice_row, payment, order
):
    with scopes_disabled():
        manual = order.payments.create(
            provider="manual", state="created", amount=order.total
        )
    assert (
        client.get(
            url(payment).replace(f"/{payment.pk}/", f"/{manual.pk}/")
        ).status_code
        == 404
    )


@pytest.mark.django_db
def test_open(client, invoice_row, payment):
    response = client.get(url(payment))
    assert response.status_code == 200
    html = response.content.decode()
    assert "<svg" in html and 'role="img"' in html
    assert f'href="{invoice_row.payment_uri}"' in html
    assert invoice_row.bolt11 in html
    assert "0.00049 BTC" in html and "49,000 sats" in html
    assert 'data-poll-interval="3"' in html
    assert "pretix_sattleme/pay.js" in html
    assert response["X-Robots-Tag"] == "noindex"
    assert "no-store" in response["Cache-Control"]
    # No script of this page runs inline (pretix's Content Security Policy).
    assert "<script>" not in html and 'onclick="' not in html


@pytest.mark.django_db
def test_the_page_escapes_the_invoice(client, payment):
    """Whatever the stored invoice holds (the SDK checked it): text on the page, in its
    attributes too, never markup."""
    mark = '"><script>alert(1)</script>'
    data = make_invoice(onchain=True).to_json()
    for field in ("bolt11", "onchainAddress", "paymentUri"):
        data[field] += mark
    with scopes_disabled():
        BitcoinInvoice.objects.create(
            payment=payment, ref=data["ref"], data=data, next_check=now()
        )
    response = client.get(url(payment))
    assert response.status_code == 200
    html = response.content.decode()
    assert "<script>alert(1)</script>" not in html
    assert html.count("&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;") >= 3


@pytest.mark.django_db
def test_open_onchain(client, payment):
    invoice = make_invoice(onchain=True)
    with scopes_disabled():
        BitcoinInvoice.objects.create(
            payment=payment, ref=invoice.ref, data=invoice.to_json(), next_check=now()
        )
    html = client.get(url(payment)).content.decode()
    assert invoice.onchain_address in html
    assert "send exactly 0.00049 BTC" in html
    assert "about 30 minutes" in html


@pytest.mark.django_db
def test_underpaid_shows_the_rest(client, invoice_row, payment):
    rest = Rest(
        sats=19_000, bolt11="lnbc190u1rest", payment_uri="lightning:lnbc190u1rest"
    )
    set_status(invoice_row, PaymentStatus.open(invoice_row.expires_at, 30_000, rest))
    html = client.get(url(payment)).content.decode()
    assert "We received 30,000 sats" in html
    assert "remaining 19,000 sats" in html
    assert 'href="lightning:lnbc190u1rest"' in html
    assert invoice_row.bolt11 not in html


@pytest.mark.django_db
def test_underpaid_waits_for_the_rest_invoice(client, invoice_row, payment):
    set_status(
        invoice_row,
        PaymentStatus.open(invoice_row.expires_at, 30_000, Rest(19_000, None, None)),
    )
    html = client.get(url(payment)).content.decode()
    assert "being prepared" in html and "<svg" not in html


@pytest.mark.django_db
def test_unconfirmed(client, invoice_row, payment):
    set_status(invoice_row, PaymentStatus.unconfirmed(49_000, 1, (TXID,)))
    html = client.get(url(payment)).content.decode()
    assert "1 of 3 confirmations" in html and "<svg" not in html
    assert 'data-poll-interval="30"' in html


@pytest.mark.django_db
def test_ended(client, payment):
    invoice = make_invoice(created=int(time.time()) - 1000)
    with scopes_disabled():
        BitcoinInvoice.objects.create(
            payment=payment, ref=invoice.ref, data=invoice.to_json(), next_check=now()
        )
    html = client.get(url(payment)).content.decode()
    assert "The time to pay this invoice is over" in html and "<svg" not in html


@pytest.mark.django_db
def test_expired(client, invoice_row, payment):
    set_status(invoice_row, PaymentStatus.expired(0))
    html = client.get(url(payment)).content.decode()
    assert "expired without being paid" in html and "data-poll-interval" not in html

    set_status(invoice_row, PaymentStatus.expired(30_000))
    html = client.get(url(payment)).content.decode()
    assert "we received 30,000 of 49,000 sats" in html


@pytest.mark.django_db
def test_canceled(client, invoice_row, payment):
    with scopes_disabled():
        OrderPayment.objects.filter(pk=payment.pk).update(state="canceled")
    html = client.get(url(payment)).content.decode()
    assert "This payment was canceled" in html and "<svg" not in html


@pytest.mark.django_db
def test_paid_goes_to_the_order(client, invoice_row, payment, order):
    set_status(invoice_row, PaymentStatus.paid_lightning("cd" * 32))
    with scopes_disabled():
        OrderPayment.objects.filter(pk=payment.pk).update(state="confirmed")
    response = client.get(url(payment))
    assert response.status_code == 302
    assert response["Location"].endswith(
        f"/order/{order.code}/{order.secret}/?paid=yes"
    )


@pytest.mark.django_db
def test_arrived_waits_for_pretix_to_confirm_the_payment(
    client, fake_sdk, invoice_row, payment
):
    """A check stores the SDK's `paid`, then has pretix confirm the payment: in between (that
    check, holding the invoice, under way in a worker), the page and its status endpoint say
    the payment arrived, and send the customer to the order only once pretix confirmed it.
    """
    set_status(invoice_row, PaymentStatus.paid_lightning("cd" * 32))
    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(
            lease_until=now() + timedelta(seconds=60)
        )
    response = client.get(url(payment))
    assert response.status_code == 200
    html = response.content.decode()
    assert "Your payment has arrived: your order is being updated." in html
    assert "<svg" not in html and 'data-poll-interval="3"' in html
    answer = client.get(url(payment, "status/")).json()
    assert answer["phase"] == "arrived" and "redirect" not in answer
    assert fake_sdk.checked == []  # That check holds the invoice: none other runs.

    with scopes_disabled():
        OrderPayment.objects.filter(pk=payment.pk).update(state="confirmed")
    answer = client.get(url(payment, "status/")).json()
    assert answer["phase"] == "paid" and answer["redirect"].endswith("?paid=yes")


@pytest.mark.django_db
def test_no_invoice_goes_to_the_order(client, payment, order):
    response = client.get(url(payment))
    assert response.status_code == 302
    assert f"/order/{order.code}/{order.secret}/" in response["Location"]


@pytest.mark.django_db
def test_check_without_javascript(client, fake_sdk, invoice_row, payment):
    fake_sdk.statuses = [PaymentStatus.paid_lightning("cd" * 32)]
    response = client.get(url(payment, "?check=1"))
    assert response.status_code == 302 and response["Location"].endswith(url(payment))
    assert len(fake_sdk.checked) == 1
    assert client.get(url(payment)).status_code == 302  # Paid: to the order.


@pytest.mark.django_db
def test_status(client, fake_sdk, invoice_row, payment, order):
    answer = client.get(url(payment, "status/")).json()
    assert answer["phase"] == "open" and "redirect" not in answer
    html = client.get(url(payment)).content.decode()
    assert f'data-version="{answer["version"]}"' in html

    # Checked at most every few seconds.
    fake_sdk.statuses = [PaymentStatus.paid_lightning("cd" * 32)]
    assert client.get(url(payment, "status/")).json()["phase"] == "open"
    assert len(fake_sdk.checked) == 1

    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(
            checked=now() - timedelta(seconds=60)
        )
    response = client.get(url(payment, "status/"))
    assert "no-store" in response["Cache-Control"]
    answer = response.json()
    assert answer["phase"] == "paid"
    assert answer["redirect"].endswith(f"/order/{order.code}/{order.secret}/?paid=yes")
    order.refresh_from_db()
    assert order.status == "p"


@pytest.mark.django_db
def test_status_changes_with_what_the_page_shows(
    client, fake_sdk, invoice_row, payment
):
    first = client.get(url(payment, "status/")).json()["version"]
    set_status(invoice_row, PaymentStatus.unconfirmed(49_000, 1, (TXID,)))
    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(checked=now())
    second = client.get(url(payment, "status/")).json()
    assert second["phase"] == "unconfirmed" and second["version"] != first


@pytest.mark.django_db
def test_status_without_invoice(client, payment, order):
    answer = client.get(url(payment, "status/")).json()
    assert answer["phase"] == "none"
    assert f"/order/{order.code}/{order.secret}/" in answer["redirect"]


@pytest.mark.parametrize(
    "language, text",
    [
        ("de", "Scannen Sie diesen QR-Code mit Ihrem Bitcoin-Wallet."),
        ("de-informal", "Scanne diesen QR-Code mit deinem Bitcoin-Wallet."),
        ("fr", "Scannez ce code QR avec votre portefeuille Bitcoin."),
        ("it", "Scansiona questo codice QR con il tuo wallet Bitcoin."),
    ],
)
@pytest.mark.django_db
def test_translated(client, invoice_row, payment, event, language, text):
    event.settings.set("locales", ["en", language])
    event.settings.set("locale", language)
    html = client.get(url(payment), HTTP_ACCEPT_LANGUAGE=language).content.decode()
    assert text in html


@pytest.mark.django_db
def test_a_rest_invoice_too_long_for_a_qr_code(client, invoice_row, payment):
    long = "lnbc190u1" + "x" * 3000
    rest = Rest(sats=19_000, bolt11=long, payment_uri="lightning:" + long)
    set_status(invoice_row, PaymentStatus.open(invoice_row.expires_at, 30_000, rest))
    response = client.get(url(payment))
    assert response.status_code == 200
    html = response.content.decode()
    assert "<svg" not in html and 'href="lightning:' + long + '"' in html
