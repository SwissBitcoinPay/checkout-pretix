"""Making the invoice of a payment.

The record of a payment's invoice is stored first, with a random reference (`record`): the SDK
is asked for an invoice of that reference only once it is committed. Then the invoice is made
with the payment and its order locked in the database (`create`), so that:

- two requests for one payment at once (a double click, two workers) get one invoice;
- a try that follows a failed one (a timeout, a worker stopped, a database restored) asks for
  the same reference again, and the SDK hands out the invoice it may have made meanwhile
  (`InvoicePendingError`) or reports it paid (`OrderPaidError`) instead of making another one;
- an order never gets a second invoice while another one of it may still be paid.
"""

import logging
import secrets
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn

from django.db import transaction
from django.utils.formats import date_format
from django.utils.timezone import now
from django.utils.translation import gettext, gettext_lazy as _
from pretix.base.i18n import language
from pretix.base.models import Event, Order, OrderPayment
from pretix.base.payment import PaymentException
from pretix.helpers import OF_SELF
from sattleme_checkout import (
    InvalidOrderError,
    InvoicePendingError,
    NetworkError,
    OnchainUnavailableError,
    OrderPaidError,
    Price,
    RateError,
    UnsupportedCurrencyError,
)

from . import checks, prices, sdk
from .models import GRACE_SECONDS, BitcoinInvoice

logger = logging.getLogger(__name__)

# The SDK's bounds of an invoice's duration, of its note and of an order's reference.
DURATION_MIN = 60
DURATION_MAX = 3600
NOTE_MAX = 100
REF_MAX = 64
# How many minutes the customer has to pay an invoice, unless the organizer chose otherwise.
DEFAULT_MINUTES = 15

# An invoice ends, its grace period included, at least this long before the order's deadline
# (seconds): its last checks, a check after its end among them, come before pretix expires the
# order, so that an on-chain payment they see keeps it from expiring (checks._hold). With Celery
# workers they come seconds after the end; without, when cron runs pretix's periodic tasks.
DEADLINE_MARGIN = 300

# A request that waited longer than this (seconds) for another request on the same payment,
# which made no invoice, makes none either: two calls to the SDK in a row could outlast the
# request (gunicorn stops a worker after 30 s by default).
LOCK_WAIT = 5

# Errors after which the same request may succeed a moment later.
TRANSIENT_ERRORS = (NetworkError, RateError, OnchainUnavailableError, TimeoutError)
# Errors of the order itself: its amount or its currency.
ORDER_ERRORS = (InvalidOrderError, UnsupportedCurrencyError)

TOO_LATE = _(
    "The time to pay this order is almost over: it cannot be paid with Bitcoin."
)
TRY_AGAIN = _(
    "We could not create your Bitcoin invoice right now. Please try again in a moment."
)


class ClockError(Exception):
    """The invoice made ends after the deadline it was asked for: the clock of the merchant's
    Lightning address (or of this server) is off by up to the 300 s the SDK accepts."""

    code = "clock"


class MismatchError(Exception):
    """The invoice the SDK gave is not of the payment's price: never shown."""

    code = "mismatch"


def create(provider: Any, payment: OrderPayment) -> BitcoinInvoice:
    """The invoice of `payment`: the one it has, else one the SDK makes for the reference of its
    record (see the module). PaymentException when there is none to show."""
    asked = time.monotonic()
    row = record(payment)
    if row.made:
        return row
    error: Exception | None = None
    paid = False
    data: dict[str, Any] = {}
    status: dict[str, Any] | None = None
    with transaction.atomic():
        # The payment, then its order, as pretix locks them (OrderPayment.confirm,
        # change_payment_provider): every invoice of the order is made one at a time.
        locked = OrderPayment.objects.select_for_update(of=OF_SELF).get(pk=payment.pk)
        order = Order.objects.select_for_update(of=OF_SELF).get(pk=locked.order_id)
        row = _get(row.pk)
        if row.made:
            return row
        if locked.state != OrderPayment.PAYMENT_STATE_CREATED:
            raise PaymentException(_("This payment can no longer be made."))
        other = payable_invoice(order, locked)
        if other is not None:
            raise PaymentException(_busy(other, order.event))
        if time.monotonic() - asked > LOCK_WAIT:
            raise PaymentException(TRY_AGAIN)
        deadline = payment_deadline(order)
        duration = invoice_duration(provider, deadline)
        # As the method's availability reads it (prices.py); the SDK refuses a price it does
        # not take (InvalidOrderError), before any request.
        price = Price(prices.amount_text(locked.amount), provider.event.currency)
        try:
            invoice = sdk.client(provider.event).create_invoice(
                ref=row.ref,
                fiat=price,
                duration_seconds=duration,
                note=invoice_note(order),
                timeout=sdk.CREATE_TIMEOUT,
            )
        except InvoicePendingError as e:
            # An earlier try of this payment failed once the SDK had made its invoice: that
            # one, handed out again (its payment checked first by the SDK), never a second one.
            data, earlier = e.invoice.to_json(), True
        except OrderPaidError as e:
            # That invoice was paid already (this server lost it: a database restored): stored
            # with the status the SDK found, which the customer's page shows (the payment has
            # arrived, no QR code), then checked at once in the background, which confirms the
            # payment as any check does. Not in this request, which may have waited for the
            # SDK all its time already (gunicorn stops a worker after 30 s by default).
            data, earlier, paid = e.invoice.to_json(), True, True
            status = e.status.to_json()
        except Exception as e:
            # Nothing was shown: the SDK gives an invoice only once it is declared. A later
            # try asks for the same reference again.
            error = e
        else:
            data, earlier = invoice.to_json(), False
        if error is None:
            error = unfit(data, price, deadline, paid)
        if error is None:
            row.data = data
            row.status = status
            row.next_check = now()
            row.error = ""
            row.save(update_fields=["data", "status", "next_check", "error"])
            locked.state = OrderPayment.PAYMENT_STATE_PENDING
            locked.info_data = row.info()
            locked.save(update_fields=["state", "info"])
            order.log_action(
                (
                    "pretix_sattleme.invoice.recovered"
                    if earlier
                    else "pretix_sattleme.invoice.created"
                ),
                data={
                    "local_id": locked.local_id,
                    "ref": row.ref,
                    "sats": row.sats,
                    "lightning": row.bolt11 is not None,
                    "onchain": row.onchain_address is not None,
                },
            )
            checks.start_chain(row, delay=0 if paid else None)
        else:
            BitcoinInvoice.objects.filter(pk=row.pk).update(error=error_code(error))
    if error is not None:
        _refuse(order, payment, error)
    payment.refresh_from_db()
    return _get(row.pk)


def _get(pk: int) -> BitcoinInvoice:
    row: BitcoinInvoice = BitcoinInvoice.objects.get(pk=pk)
    return row


def record(payment: OrderPayment) -> BitcoinInvoice:
    """The record of `payment`'s invoice, made with a new reference when it has none: stored,
    committed, before any invoice of that reference is asked for."""
    with transaction.atomic():
        locked = OrderPayment.objects.select_for_update(of=OF_SELF).get(pk=payment.pk)
        rows = BitcoinInvoice.objects.filter(payment=locked)
        row: BitcoinInvoice | None = rows.first()
        if row is None:
            if locked.state != OrderPayment.PAYMENT_STATE_CREATED:
                raise PaymentException(_("This payment can no longer be made."))
            row = BitcoinInvoice.objects.create(payment=locked, ref=new_ref(locked))
    return row


def payable_invoice(order: Order, payment: OrderPayment) -> BitcoinInvoice | None:
    """The invoice of another payment of `order` that may still be paid, if any: an order has
    one at a time (BitcoinInvoice.may_be_paid)."""
    rows = (
        BitcoinInvoice.objects.filter(payment__order_id=order.pk, data__isnull=False)
        .exclude(payment_id=payment.pk)
        .select_related("payment")
    )
    return next((row for row in rows if row.may_be_paid()), None)


def _busy(row: BitcoinInvoice, event: Event) -> str:
    """Why `row`, another invoice of the order, keeps it from getting a new one."""
    nothing_came = not row.received and row.state not in ("unconfirmed", "paid")
    if not row.final and nothing_came and time.time() < row.end:
        end = datetime.fromtimestamp(row.end, tz=event.timezone)
        text: str = gettext(
            "An earlier Bitcoin invoice of this order can still be paid, until {time}: a new "
            "one can be made once it has ended."
        )
        return text.format(time=date_format(end, "TIME_FORMAT"))
    if not row.final and nothing_came:
        text = gettext(
            "The time to pay an earlier Bitcoin invoice of this order is over, and its payment "
            "is being checked: a new one can be made once it is known to be unpaid."
        )
        return text
    if row.final:
        text = gettext(
            "A Bitcoin payment of this order awaits the organizer: no other Bitcoin invoice "
            "can be made meanwhile. Please contact the organizer."
        )
        return text
    text = gettext(
        "A Bitcoin payment of this order has arrived and is being checked: no other Bitcoin "
        "invoice can be made meanwhile."
    )
    return text


def unfit(
    data: dict[str, Any], price: Price, deadline: datetime | None, paid: bool
) -> Exception | None:
    """Why the invoice the SDK gave must not be shown, if it must not: it is not of the
    payment's price (MismatchError), or could be paid after the deadline (ClockError; but for
    one paid already: the bitcoin came)."""
    fiat = data.get("fiat") or {}
    amount = fiat.get("amount")
    try:
        same = (
            isinstance(amount, str)
            and fiat.get("currency") == price.currency
            and Decimal(amount) == Decimal(price.amount)
        )
    except InvalidOperation:
        same = False
    if not same:
        return MismatchError(
            f"an invoice of {fiat.get('amount')} {fiat.get('currency')}, "
            f"for a payment of {price.amount} {price.currency}"
        )
    end = int(data["expiresAt"]) + GRACE_SECONDS
    if not paid and deadline is not None and end > deadline.timestamp():
        # Never shown: it would be payable past the deadline.
        return ClockError(
            f"ends at {data['expiresAt']}, deadline {deadline.isoformat()}"
        )
    return None


def error_code(error: BaseException) -> str:
    """The code an error of a try to make an invoice is stored and logged with."""
    if isinstance(error, (ClockError, MismatchError)):
        return error.code
    return sdk.error_code(error)


def _refuse(order: Order, payment: OrderPayment, error: Exception) -> NoReturn:
    """Log why no invoice could be made, then tell the customer (PaymentException)."""
    code = error_code(error)
    if isinstance(error, ClockError):
        logger.warning("No Bitcoin invoice for payment %s: %s", payment.full_id, error)
        message = TOO_LATE
    elif isinstance(error, TRANSIENT_ERRORS + ORDER_ERRORS):
        logger.warning(
            "No Bitcoin invoice for payment %s: %s",
            payment.full_id,
            sdk.describe(error),
        )
        if isinstance(error, TRANSIENT_ERRORS):
            message = TRY_AGAIN
        else:
            message = _(
                "This order cannot be paid with Bitcoin. Please choose another payment "
                "method."
            )
    else:
        # A key that is invalid or was revoked, the merchant's Lightning address answering
        # wrongly, an invoice of another price, or a fault of this plugin: the organizer has to
        # act.
        logger.error(
            "No Bitcoin invoice for payment %s: %s",
            payment.full_id,
            error if isinstance(error, MismatchError) else sdk.describe(error),
            exc_info=error if code == "error" else None,
        )
        message = _(
            "Bitcoin payments are not available right now. Please choose another payment "
            "method or contact the organizer."
        )
    order.log_action(
        "pretix_sattleme.invoice.failed",
        data={"local_id": payment.local_id, "error": code},
    )
    raise PaymentException(message)


def new_ref(payment: OrderPayment) -> str:
    """A new order reference for the SDK: 128 bits of the operating system's CSPRNG, after the
    order's code and the payment's number for whoever reads it.

    Random, so that no two payments ever share one, whatever holds the key: a deleted test
    order whose code comes back, a restored backup, another installation. The SDK would hand
    out the invoice of another payment that had it, or report it paid.
    """
    token = secrets.token_hex(16)
    code = "".join(c for c in payment.order.code if c.isascii() and c.isalnum())[:16]
    ref = f"pretix-{code}-{payment.local_id}-{token}"
    return ref if len(ref) <= REF_MAX else f"pretix-{token}"


def invoice_note(order: Order) -> str:
    """What the merchant's app shows of the sale: the event's name and the order's code."""
    with language(order.event.settings.locale):
        name = str(order.event.name)
    # The SDK refuses control characters.
    name = " ".join(
        "".join(" " if ord(c) < 0x20 or ord(c) == 0x7F else c for c in name).split()
    )
    if not name:
        return str(order.code)
    room = NOTE_MAX - len(f" · {order.code}")
    if len(name) > room:
        name = name[: room - 1].rstrip() + "…"
    return f"{name} · {order.code}"


def invoice_duration(provider: Any, deadline: datetime | None) -> int:
    """How long the invoice can be paid, in seconds: the provider's setting, but never past
    `deadline` (see payment_deadline), when pretix would refuse the payment.
    PaymentException when too little is left.

    An on-chain payment counts when it comes by the end of the invoice's grace period: the
    invoice ends that long, and DEADLINE_MARGIN more, before the deadline, so that every payment
    the SDK reports paid came in time (checks.py confirms it whatever the date its confirmations
    end), and the checks after its end come before the deadline.
    """
    minutes: int = provider.settings.get(
        "invoice_minutes", as_type=int, default=DEFAULT_MINUTES
    )
    seconds = max(DURATION_MIN, min(DURATION_MAX, minutes * 60))
    if deadline is not None:
        # The SDK dates the invoice when it makes it, up to CREATE_TIMEOUT from now: its
        # duration counts from then, and must still end, with its grace period, a margin
        # before the deadline.
        left = (
            int((deadline - now()).total_seconds())
            - GRACE_SECONDS
            - sdk.CREATE_TIMEOUT
            - DEADLINE_MARGIN
        )
        if left < DURATION_MIN:
            raise PaymentException(TOO_LATE)
        seconds = min(seconds, left)
    return seconds


def payment_deadline(order: Order) -> datetime | None:
    """When an invoice of `order` must end, if ever: a bitcoin payment cannot be refused once
    made.

    The order's last date of payments, after which pretix refuses any payment. And, while the
    order is pending, the moment pretix expires it, which frees its products for other
    customers: unless that moment passed already and late payments are accepted (the payment
    is then a late payment, as when the order expired).
    """
    deadlines: list[datetime] = []
    if order.payment_term_last:
        deadlines.append(order.payment_term_last)
    settings = order.event.settings
    if (
        order.status == Order.STATUS_PENDING
        and settings.get("payment_term_expire_automatically", as_type=bool)
        and not order.valid_if_pending
    ):
        expiry = order.payment_term_expire_date
        if expiry > now() or not settings.get("payment_term_accept_late", as_type=bool):
            deadlines.append(expiry)
    return min(deadlines) if deadlines else None
