"""What the templates show of an invoice."""

import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from django.utils.formats import number_format
from django.utils.translation import gettext, gettext_lazy as _
from pretix.base.models import OrderPayment

from . import qr
from .models import BitcoinInvoice

# What the SDK writes as a payment URI: a BIP-21 URI, or a Lightning one.
URI_SCHEMES = ("bitcoin:", "lightning:")
TXID = re.compile("[0-9a-f]{64}")
ADDRESS = re.compile("[A-Za-z0-9]{14,90}")

# Phases of an invoice whose page waits for news, and how often it asks (seconds).
POLL_INTERVALS = {
    "open": 3,
    "underpaid": 3,
    "arrived": 3,
    "ended": 5,
    "unconfirmed": 30,
    # Until the organizer confirms the payment by hand.
    "review": 30,
}

# The errors of a try to make an invoice, or of a check (BitcoinInvoice.error), in plain words
# for the organizer: the SDK's codes (SPEC.md §5), and the plugin's own.
ERRORS = {
    "network": _(
        "the merchant's relays, its Lightning address, a source of exchange rates or a "
        "blockchain explorer did not answer"
    ),
    "timeout": _("it took too long"),
    "invalid_key": _("the integration key is not valid"),
    "revoked": _("the integration was removed in the sattle.me app"),
    "channel_not_found": _("the integration could not be found on its relays"),
    "invalid_invoice": _(
        "the merchant's Lightning address answered with an invoice that failed the checks"
    ),
    "invalid_order": _(
        "the order's amount or currency was refused, or the invoice stored is not valid"
    ),
    "unsupported_currency": _(
        "no source of exchange rates prices the event's currency"
    ),
    "rate": _("no usable exchange rate could be found"),
    "onchain_unavailable": _(
        "this amount can only be paid on-chain, and no deposit address of the integration "
        "was free"
    ),
    "clock": _(
        "the invoice would have ended after the order's deadline: a clock is off, the "
        "merchant's Lightning address's or this server's"
    ),
    "mismatch": _("the invoice handed out was not of the payment's price"),
    "apply": _("pretix could not apply what the check found (see the server's log)"),
    "error": _("an unexpected error (see the server's log)"),
}


def error_text(code: str) -> str:
    """An error's code in plain words (the code itself when it is not known)."""
    return str(ERRORS.get(code, code))


def btc(sats: int) -> str:
    """An amount in sats, in bitcoins, exactly: 49000 is 0.00049."""
    return format((Decimal(sats) / 100_000_000).normalize(), "f")


def sats(value: int) -> str:
    return str(number_format(value, force_grouping=True))


def _uri(value: object) -> str | None:
    return value if isinstance(value, str) and value.startswith(URI_SCHEMES) else None


def _time(seconds: int | None) -> datetime | None:
    return datetime.fromtimestamp(seconds, tz=timezone.utc) if seconds else None


def context(
    row: BitcoinInvoice,
    payment: OrderPayment,
    now: int | None = None,
    with_qr: bool = True,
) -> dict[str, Any]:
    """What the customer's pages show of the invoice and its payment (`with_qr`: its QR
    code too, for the payment page)."""
    now = int(time.time()) if now is None else now
    phase = row.phase(payment, now)
    rest = row.rest or {}
    if phase == "underpaid":
        uri = _uri(rest.get("paymentUri"))
        lightning = rest.get("bolt11")
        amount = int(rest["sats"]) if rest.get("sats") else 0
    else:
        uri = _uri(row.payment_uri)
        lightning = row.bolt11
        amount = row.sats
    # On-chain to the invoice's address: its BIP-21 URI says so (a rest below 1000 sats is
    # payable in Lightning only).
    onchain = bool(uri and uri.startswith("bitcoin:") and row.onchain_address)
    show = phase in ("open", "underpaid")
    left = max(0, row.expires_at - now)
    return {
        "invoice": row,
        "payment": payment,
        "order": payment.order,
        "event": payment.order.event,
        "phase": phase,
        "sats": sats(row.sats),
        "btc": btc(row.sats),
        "received_sats": sats(row.received),
        "to_pay_sats": sats(amount),
        "to_pay_btc": btc(amount),
        "uri": uri if show else None,
        "qr": (
            qr.svg(uri, gettext("QR code of the Bitcoin payment"))
            if with_qr and show and uri
            else None
        ),
        "lightning": lightning if show else None,
        "address": row.onchain_address if show and onchain else None,
        "has_onchain": row.onchain_address is not None,
        "seconds_left": left,
        "time_left": f"{left // 60}:{left % 60:02d}",
        "expires": _time(row.expires_at),
        "ends": _time(row.end),
        "confirmations": row.confirmations or 0,
        "poll_interval": POLL_INTERVALS.get(phase),
    }


def control_context(row: BitcoinInvoice, payment: OrderPayment) -> dict[str, Any]:
    """What the organizer's backend shows of the invoice and its checks; of a record whose
    invoice was not made, its reference and why."""
    # Not errors: the template says them in its own words.
    error = (
        error_text(row.error)
        if row.error and row.error not in ("abandoned", "changed")
        else None
    )
    if not row.made:
        return {"invoice": row, "payment": payment, "error": error}
    status = row.status or {}
    rate = row.data.get("rate") or {}
    txids = [t for t in status.get("txids") or () if TXID.fullmatch(str(t))]
    preimages = [status["preimage"]] if status.get("preimage") else []
    preimages += list(status.get("preimages") or ())
    address = row.onchain_address
    return {
        "invoice": row,
        "payment": payment,
        "phase": row.phase(payment),
        "sats": sats(row.sats),
        "btc": btc(row.sats),
        "fiat": row.data.get("fiat"),
        "rate": rate,
        "rate_time": _time(rate.get("fetchedAt")),
        "created_at": _time(row.data.get("createdAt")),
        "expires": _time(row.expires_at),
        "address": address,
        "address_link": bool(address and ADDRESS.fullmatch(address)),
        "state": status.get("state"),
        "awaits_organizer": row.awaits_organizer(payment),
        "method": status.get("method"),
        "received_sats": sats(row.received),
        "confirmations": status.get("confirmations"),
        "txids": txids,
        "preimages": preimages,
        "error": error,
    }
