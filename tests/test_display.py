import re
from xml.etree import ElementTree

import pytest
import qrcode
from qrcode.constants import ERROR_CORRECT_M
from sattleme_checkout import (
    Client,
    InvalidKeyError,
    NetworkError,
    PaymentStatus,
)

from pretix_sattleme import display, qr, sdk
from pretix_sattleme.models import BitcoinInvoice

from .conftest import KEY, make_invoice


def dark_modules(svg):
    element = ElementTree.fromstring(svg).find("{http://www.w3.org/2000/svg}path")
    assert element is not None
    path = element.get("d")
    assert path is not None
    modules: set[tuple[int, int]] = set()
    for x, y, run in re.findall(r"M(\d+) (\d+)h(\d+)v1h-\d+z", path):
        modules.update((int(x) + i, int(y)) for i in range(int(run)))
    return modules


@pytest.mark.parametrize(
    "text",
    [
        "lightning:lnbc490u1p5" + "q" * 300,
        "bitcoin:bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq?amount=0.00049&lightning=lnbc1",
        "x",
    ],
)
def test_qr_is_the_code_of_the_text(text):
    svg = qr.svg(text, 'QR "code"')
    assert svg is not None
    root = ElementTree.fromstring(svg)
    assert root.get("aria-label") == 'QR "code"'
    code = qrcode.QRCode(error_correction=ERROR_CORRECT_M, border=qr.QUIET_ZONE)
    code.add_data(text)
    code.make(fit=True)
    matrix = code.get_matrix()
    assert root.get("viewBox") == f"0 0 {len(matrix)} {len(matrix)}"
    expected = {
        (x, y) for y, row in enumerate(matrix) for x, cell in enumerate(row) if cell
    }
    assert dark_modules(svg) == expected


def test_qr_of_a_text_too_long():
    assert qr.svg("lightning:lnbc1" + "q" * 3000, "QR") is None


@pytest.mark.parametrize(
    "sats, text",
    [
        (1, "0.00000001"),
        (49_000, "0.00049"),
        (100_000_000, "1"),
        (2_100_000_000_000_000, "21000000"),
    ],
)
def test_btc(sats, text):
    assert display.btc(sats) == text


@pytest.mark.django_db
def test_only_payment_uris_are_linked(payment):
    invoice = make_invoice().to_json()
    invoice["paymentUri"] = "javascript:alert(1)"
    row = BitcoinInvoice(payment=payment, ref="r", data=invoice)
    context = display.context(row, payment)
    assert context["phase"] == "open"
    assert context["uri"] is None and context["qr"] is None


@pytest.mark.django_db
def test_phases(payment):
    row = BitcoinInvoice(payment=payment, ref="r", data=make_invoice().to_json())
    end = row.expires_at
    assert row.phase(payment, end - 1) == "open"
    assert row.phase(payment, end) == "ended"
    row.status = PaymentStatus.unconfirmed(49_000, 0, ()).to_json()
    payment.state = "canceled"
    assert row.phase(payment, end - 1) == "unconfirmed"
    row.status = None
    assert row.phase(payment, end - 1) == "canceled"
    # The SDK reported it paid: the customer is told it arrived, and sent to the order only
    # once pretix confirmed the payment (a check confirms it right after storing the status).
    row.status = PaymentStatus.paid_lightning("cd" * 32).to_json()
    payment.state = "pending"
    assert row.phase(payment, end - 1) == "arrived"
    assert row.phase(payment, end + 1000) == "arrived"
    payment.state = "confirmed"
    assert row.phase(payment, end - 1) == "paid"
    row.status = None
    assert row.phase(payment, end - 1) == "paid"


def test_client_options_are_valid():
    """The real SDK accepts the plugin's options (an invalid one fails at construction)."""
    Client(
        KEY, relay_timeout_ms=sdk.RELAY_TIMEOUT_MS, http_timeout_ms=sdk.HTTP_TIMEOUT_MS
    )
    assert repr(Client(KEY)) == "<Client>"


@pytest.mark.django_db
def test_client_of_an_event_without_key(event, monkeypatch):
    monkeypatch.setattr("pretix_sattleme.sdk.Client", Client)
    event.settings.delete(sdk.KEY_SETTING)
    with pytest.raises(InvalidKeyError):
        sdk.client(event)
    event.settings.set(sdk.KEY_SETTING, KEY)
    assert repr(sdk.client(event)) == "<Client>"


def test_errors_never_show_more_than_their_code_and_message():
    assert sdk.error_code(NetworkError("no relay")) == "network"
    assert sdk.describe(NetworkError("no relay")) == "network: no relay"
    assert sdk.error_code(TimeoutError()) == "timeout"
    assert sdk.error_code(ValueError(KEY)) == "error"
    assert KEY not in sdk.describe(ValueError(KEY))
