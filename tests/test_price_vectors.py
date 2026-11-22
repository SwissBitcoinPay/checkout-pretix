"""The plugin's check of a price (prices.py) and the SDK's, on the SDK's own vectors of SPEC.md §25
and §4 (sdk/spec/vectors/fiat.json, pinned with the SDK): they agree on every case, so that the
method is offered exactly for the prices the SDK takes.

The SDK's check is `create_invoice` itself, as the plugin calls it: a price it refuses is
`InvalidOrderError` naming `fiat`, before any request; a price it takes goes on to the merchant's
relays, here a connection refused at once (`NetworkError`). Never the network.
"""

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest
from sattleme_checkout import (
    Client,
    InvalidOrderError,
    NetworkError,
    Price,
    currency_digits,
)

from pretix_sattleme import prices
from pretix_sattleme.payment import Sattleme

from .conftest import KEY

VECTORS = json.loads(
    (
        Path(__file__).resolve().parent.parent
        / "sdk"
        / "spec"
        / "vectors"
        / "fiat.json"
    ).read_text(encoding="utf-8")
)


def case_id(case):
    given = case["input"]
    return f"{given['amount']!r} {given['currency']!r}"


def is_decimal(text):
    try:
        Decimal(text)
    except InvalidOperation:
        return False
    return True


# The cases whose amount is one pretix could hold (a Decimal; the others are its text's alone).
AMOUNTS = [c for c in VECTORS["cases"] if is_decimal(c["input"]["amount"])]


class Requests:
    """The SDK's HTTP and WebSocket clients: each request recorded, and refused."""

    def __init__(self):
        self.urls = []

    async def websocket(self, url):
        self.urls.append(url)
        raise OSError("no network in the tests")

    async def __call__(self, url, *, headers, socket_timeout, max_bytes):
        self.urls.append(url)
        raise OSError("no network in the tests")


def sdk_takes(amount, currency):
    """Whether the real SDK takes the price `amount` of `currency` for an invoice."""
    requests = Requests()
    client = Client(
        KEY,
        http=requests,
        websocket=requests.websocket,
        relay_timeout_ms=100,
        http_timeout_ms=100,
    )
    try:
        client.create_invoice(
            ref="price-vectors", fiat=Price(amount, currency), timeout=10
        )
    except InvalidOrderError as e:
        assert e.param == "fiat"
        assert requests.urls == []  # Refused before any request.
        return False
    except NetworkError:
        assert requests.urls  # Taken: it went on to the relays.
        return True
    raise AssertionError("the SDK made an invoice without the network")


def test_the_vectors_are_there():
    assert len(VECTORS["cases"]) >= 30 and len(VECTORS["digits"]) >= 20
    assert len(AMOUNTS) >= 20


@pytest.mark.parametrize("case", VECTORS["cases"], ids=case_id)
def test_a_price_is_taken_by_the_plugin_as_by_the_sdk(case):
    amount, currency = case["input"]["amount"], case["input"]["currency"]
    expected = case["expected"]
    refused = "error" in expected
    assert refused == (expected.get("error") == "invalid_order")

    minor = prices.minor_units(amount, currency)
    assert minor == (None if refused else int(expected["minor"]))
    assert sdk_takes(amount, currency) == (not refused)


@pytest.mark.parametrize(
    "amount",
    [
        "\u0662\u0664.\u0665\u0660",  # Arabic-Indic digits, which Python's \d takes.
        "\uff12\uff14.\uff15\uff10",  # Fullwidth digits.
        "24.50\n",
        "24.5\u00a0",
    ],
)
def test_a_price_of_other_characters_is_refused_by_both(amount):
    """Beyond the vectors: what the specification's ASCII digits leave out."""
    assert prices.minor_units(amount, "CHF") is None
    assert not sdk_takes(amount, "CHF")


@pytest.mark.django_db
@pytest.mark.parametrize("case", AMOUNTS, ids=case_id)
def test_the_method_is_offered_for_an_amount_as_the_sdk_takes_it(case, event):
    """Through the provider, for an event in the case's currency: an amount of pretix (a
    Decimal) is offered exactly when the SDK takes the text the plugin sends it."""
    amount, currency = case["input"]["amount"], case["input"]["currency"]
    value = Decimal(amount)
    event.currency = currency
    provider = Sattleme(event)
    sent = prices.price(value, currency)
    assert provider._takes(value) == (sent is not None)
    assert provider._takes(value) == sdk_takes(prices.amount_text(value), currency)
    if sent is not None:
        assert sent == Price(prices.amount_text(value), currency)


@pytest.mark.parametrize("case", VECTORS["digits"], ids=lambda c: repr(c["code"]))
def test_a_currency_is_offered_by_the_plugin_as_the_sdk_prices_it(case):
    code, expected = case["code"], case["expected"]
    assert currency_digits(code) == expected
    assert prices.usable(code) == (expected is not None)
    # One unit, and one minor unit, of that currency: its decimals.
    if expected is None:
        assert prices.minor_units("1", code) is None
    else:
        assert prices.minor_units("1", code) == 10**expected
        smallest = "1" if expected == 0 else "0." + "0" * (expected - 1) + "1"
        assert prices.minor_units(smallest, code) == 1
        assert prices.minor_units("0." + "0" * expected + "1", code) is None
    assert sdk_takes("1", code) == (expected is not None)
