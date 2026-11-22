"""Which prices the SDK takes, so that the method is offered for those alone.

`create_invoice` takes an order's price in a currency of the specification's table, which the
SDK's public `currency_digits` reads, and refuses any other price before any request
(`InvalidOrderError`). Its rule (SPEC.md §25, "The price of an order"; the SDK's README, "Prices
in a currency"): the amount is 1 to 15 digits, then optionally a point and 1 to 15 digits,
nothing around them; its fraction, without trailing zeros, has at most the currency's decimals;
the amount in minor units is from 1 to 2^53 − 1 (SPEC.md §7).

pretix rounds every amount of an event to its currency's places (`CURRENCY_PLACES`, 2 unless 0)
and stores it with 2 decimals: in a currency the SDK prices with fewer decimals (0 for ISK, RSD
and others, where pretix has 2), only some of its amounts are prices the SDK takes.
"""

import re
from decimal import Decimal

from django.conf import settings
from sattleme_checkout import Price, currency_digits

# SPEC.md §25: ASCII digits alone (Python's \d would take any script's).
AMOUNT = re.compile("([0-9]{1,15})(?:\\.([0-9]{1,15}))?")
# SPEC.md §7: the largest integer every JSON reader holds exactly.
MINOR_MAX = 2**53 - 1


def amount_text(amount: Decimal) -> str:
    """How the plugin writes an amount of pretix for the SDK: as it is, never in exponent form."""
    return format(amount, "f")


def minor_units(amount: str, currency: str) -> int | None:
    """The price `amount` of `currency` in minor units, as the SDK reads it; None when the SDK
    would refuse it."""
    digits = currency_digits(currency)
    if digits is None:
        return None
    match = AMOUNT.fullmatch(amount)
    if match is None:
        return None
    whole, fraction = match.group(1), (match.group(2) or "").rstrip("0")
    if len(fraction) > digits:
        return None
    minor = int(whole) * 10**digits + int(fraction.ljust(digits, "0") or "0")
    return minor if 1 <= minor <= MINOR_MAX else None


def price(amount: Decimal, currency: str) -> Price | None:
    """The price the SDK is asked for an amount of pretix in `currency`, or None when it would
    refuse it."""
    text = amount_text(amount)
    return Price(text, currency) if minor_units(text, currency) is not None else None


def usable(currency: str) -> bool:
    """Whether some of the event's amounts in `currency` are prices the SDK takes."""
    return currency_digits(currency) is not None


def whole_units_only(currency: str) -> bool:
    """Whether the SDK prices `currency` with fewer decimals than pretix rounds its amounts to
    (pretix's CURRENCY_PLACES, 2 unless listed): only its amounts in whole units are then prices
    the SDK takes (0 decimals being the only fewer there are)."""
    digits = currency_digits(currency)
    places: int = settings.CURRENCY_PLACES.get(currency, 2)
    return digits is not None and digits < places
