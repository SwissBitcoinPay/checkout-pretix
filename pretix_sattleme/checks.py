"""Checking the payments of invoices, and applying what the SDK finds to pretix's payments.

An invoice is checked:

- for its customer's payment page, every few seconds while the page is open (`request_check`);
- by a chain of Celery tasks of its own, each one checking it and scheduling the next one
  (`start_chain`, `run_chain`): every minute while it can be paid, once just before the end of
  its grace period (an on-chain payment seen by then came in time) and once just after (its
  final status), then every five minutes until that final status is known (an on-chain
  payment's confirmations);
- by pretix's periodic tasks (`sweep`), which restart a chain that stopped (a lost task) and,
  when pretix runs without Celery workers, run the checks that are due themselves.

Two checks of one invoice never run at once (a lease in the database, which only the check
holding it writes through), and checks in a row are harmless: the SDK's checks are made to be
repeated, and settling a payment is idempotent. Only a `paid` status of the SDK, for the
invoice the payment holds, confirms a payment.
"""

import logging
import time
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import F, Q
from django.utils.timezone import now
from django_scopes import scope, scopes_disabled
from pretix.base.models import Order, OrderPayment, Quota
from pretix.base.services.orders import OrderError, extend_order
from pretix.helpers import OF_SELF
from sattleme_checkout import PaymentStatus

from . import sdk, shredding
from .models import BitcoinInvoice

logger = logging.getLogger(__name__)

# Seconds between two background checks of an invoice that can still be paid, and of one past
# its end whose final status is not known yet (an on-chain payment being confirmed).
OPEN_INTERVAL = 60
SETTLING_INTERVAL = 300
# The last check of an invoice's grace period runs this long before its end, the first one
# after it this long after (seconds). More than WEB_INTERVAL, the shortest delay of a chain
# (see next_delay): else that last check could come after the end.
END_MARGIN = 10
# A customer's payment page has its invoice checked at most this often (seconds).
WEB_INTERVAL = 6
# A check run during a web request (pretix's workers are stopped after 30 s by default).
WEB_TIMEOUT = 20
# After failed checks, the next one waits twice as long each time, up to this (seconds).
MAX_BACKOFF = 1800
# A chain whose next check is this late has stopped: the periodic tasks start a new one.
STALE_CHAIN = timedelta(minutes=10)
# An invoice whose final status is still unknown this long after it was made is given up on: a
# payment made in time whose confirmations take longer (a network congested for months) is
# then confirmed by hand.
GIVE_UP_AFTER = timedelta(days=90)
# A check holds its invoice this much longer than its call to the SDK may take.
LEASE_MARGIN = timedelta(seconds=30)
# While an on-chain payment that came in time is being confirmed, its order is kept from
# expiring: when it would expire within HOLD_MIN, or did, it is extended to HOLD from now.
HOLD_MIN = timedelta(hours=1)
HOLD = timedelta(hours=2)
# Invoices handled per run of the periodic tasks: restarted chains, or checks run inline.
SWEEP_BATCH = 200
INLINE_BATCH = 20
# A run of the periodic tasks starts no check inline (without Celery workers) whose call to the
# SDK could end later than this after the run's first one (seconds): pretix runs its other
# periodic tasks after this one.
INLINE_BUDGET = 60


def check(
    invoice_id: int, *, min_interval: int = 0, timeout: int = sdk.CHECK_TIMEOUT
) -> bool:
    """Check the payment of an invoice now, and apply what was found to its pretix payment.

    Skipped (False) when the invoice is final or not made yet, when another check of it is under
    way, or when one ended less than `min_interval` seconds ago. `timeout`: of the SDK's call,
    in seconds.
    """
    start = now()
    lease = start + timedelta(seconds=timeout) + LEASE_MARGIN
    with scopes_disabled():
        claim = BitcoinInvoice.objects.filter(
            pk=invoice_id, final=False, data__isnull=False
        ).filter(Q(lease_until__isnull=True) | Q(lease_until__lt=start))
        if min_interval:
            claim = claim.filter(
                Q(checked__isnull=True)
                | Q(checked__lte=start - timedelta(seconds=min_interval))
            )
        if not claim.update(lease_until=lease):
            return False
        row = BitcoinInvoice.objects.select_related(
            "payment__order__event__organizer"
        ).get(pk=invoice_id)
    event = row.payment.order.event
    try:
        status = sdk.client(event).get_status(row.data, timeout=timeout)
    except Exception as e:
        _failed(row, lease, e, sdk.error_code(e))
        return True
    try:
        with scope(organizer=event.organizer):
            _settle(row, lease, status)
    except Exception as e:
        # pretix could not take the update (a database error, a fault of a plugin reacting to
        # the payment): the invoice is not final, a later check applies its status again.
        _failed(row, lease, e, "apply")
    return True


def _failed(row: BitcoinInvoice, lease: datetime, error: Exception, code: str) -> None:
    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=row.pk, lease_until=lease).update(
            error=code, errors=F("errors") + 1, checked=now(), lease_until=None
        )
    # Logged when it starts failing, or fails otherwise: not at each of its retries.
    if code == row.error:
        return
    if code == "apply":
        logger.error(
            "Applying the status of Bitcoin invoice %s (payment %s) failed",
            row.ref,
            row.payment.full_id,
            exc_info=error,
        )
    else:
        (logger.error if code == "error" else logger.warning)(
            "The check of Bitcoin invoice %s (payment %s) failed: %s",
            row.ref,
            row.payment.full_id,
            sdk.describe(error),
            exc_info=error if code == "error" else None,
        )


def _settle(row: BitcoinInvoice, lease: datetime, status: PaymentStatus) -> None:
    """Store what a check found and update the pretix payment: confirmed when paid, failed
    when expired. The invoice is final only once its payment was updated, so that a check
    that stops half-way is simply done again."""
    with transaction.atomic(), scopes_disabled():
        locked = BitcoinInvoice.objects.select_for_update(of=OF_SELF).get(pk=row.pk)
        if locked.final or locked.lease_until != lease:
            return  # Final meanwhile, or this check outlived its lease: another one decides.
        previous = locked.state
        locked.status = status.to_json()
        locked.checked = now()
        locked.error = ""
        # Its failed checks in a row are reset once the status is applied: a failure to apply
        # it counts as one more (see next_delay).
        locked.save(update_fields=["status", "checked", "error"])
    payment = OrderPayment.objects.select_related("order__event").get(
        pk=locked.payment_id
    )
    settled = False
    if status.state == "paid":
        settled = _paid(locked, payment)
    elif status.state == "expired":
        settled = _expired(locked, payment)
    elif status.state == "unconfirmed":
        if previous != "unconfirmed":
            payment.order.log_action(
                "pretix_sattleme.payment.unconfirmed",
                data={
                    "local_id": payment.local_id,
                    "received": str(locked.received),
                    "confirmations": locked.confirmations,
                },
            )
        _hold(payment.order, first=previous != "unconfirmed")
    # Released only now: no other check settles the payment meanwhile. Never made non-final.
    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=locked.pk, lease_until=lease).update(
            lease_until=None, errors=0, **({"final": True} if settled else {})
        )
    if settled:
        shredding.finished(locked)


def _paid(row: BitcoinInvoice, payment: OrderPayment) -> bool:
    if payment.state == OrderPayment.PAYMENT_STATE_REFUNDED:
        return True
    if payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED:
        _finish(payment)
    else:
        # What the order owes now: its total (none once canceled), less what its other
        # payments paid (a gift card, for one), net of what was refunded. The invoice was made
        # for what it owed then: when that changed since (the order was changed, canceled, or
        # paid otherwise), the organizer settles the difference and confirms by hand.
        owed = Order.objects.get(pk=payment.order_id).pending_sum
        if Decimal(row.data["fiat"]["amount"]) != owed:
            _changed(row, payment, owed)
            _noted_amount(row, payment)
            return True
        payment.info_data = row.info()
        try:
            # Paid in time: by the invoice's end, which comes before pretix's deadlines (see
            # invoices.invoice_duration), however long its confirmations took. pretix still
            # checks that an expired order's products are available.
            payment.confirm(ignore_date=True)
        except Quota.QuotaExceededException as e:
            # pretix recorded the payment as confirmed, but could not mark the order paid (its
            # products are no longer available): the organizer decides.
            _not_marked_paid(payment, e)
    _noted_amount(row, payment)
    return True


def _changed(row: BitcoinInvoice, payment: OrderPayment, owed: Decimal) -> None:
    """The invoice was paid, and the order owes another amount than its price now: its payment
    stays pending, for the organizer to confirm by hand once the difference is settled (pretix
    has no state for an order the organizer must look at). Its customer is told so, and can
    neither choose another method nor get a new invoice meanwhile
    (BitcoinInvoice.awaits_organizer)."""
    fiat = row.data["fiat"]
    payment.info_data = row.info()
    payment.save(update_fields=["info"])
    payment.order.log_action(
        "pretix_sattleme.payment.changed",
        data={
            "local_id": payment.local_id,
            "amount": fiat["amount"],
            "currency": fiat["currency"],
            "received": str(row.received or row.sats),
            "owed": str(owed),
        },
    )
    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=row.pk).update(error="changed")
    logger.warning(
        "Bitcoin invoice %s (payment %s) paid, %s %s, while its order owes %s: left to the "
        "organizer",
        row.ref,
        payment.full_id,
        fiat["amount"],
        fiat["currency"],
        owed,
    )


def _noted_amount(row: BitcoinInvoice, payment: OrderPayment) -> None:
    """A payment of more than the invoice's sats (an overpayment), or of less, which the
    merchant accepted in the sattle.me app (its tolerance, or its acceptance of what an order
    underpaid on-chain received, by hand): noted on the order. Never held for the difference:
    the SDK's `paid` says the merchant takes it."""
    if row.received and row.received != row.sats:
        payment.order.log_action(
            "pretix_sattleme.payment.amount",
            data={
                "local_id": payment.local_id,
                "received": str(row.received),
                "sats": str(row.sats),
            },
        )


def _not_marked_paid(payment: OrderPayment, error: Exception) -> None:
    """Say why the order of a confirmed payment is not paid. pretix's own log entry of it is
    undone with the step that failed: it is logged again here, where it stays."""
    payment.order.log_action(
        "pretix.event.order.quotaexceeded", {"message": str(error)}
    )
    logger.warning(
        "Bitcoin payment %s confirmed, order not marked paid: %s",
        payment.full_id,
        error,
    )


def _finish(payment: OrderPayment) -> None:
    """Mark the order paid when its payment is confirmed and the order is not.

    pretix confirms a payment, then marks its order paid, in two steps: a failure between them
    (a plugin reacting to the payment, the database) leaves a confirmed payment and an unpaid
    order. This takes the second step again, as pretix's backend does."""
    with transaction.atomic():
        order = Order.objects.select_for_update(of=OF_SELF).get(pk=payment.order_id)
        if (
            order.status not in (Order.STATUS_PENDING, Order.STATUS_EXPIRED)
            or order.pending_sum > 0
        ):
            return
        payment = order.payments.get(pk=payment.pk)
        try:
            payment._mark_order_paid(
                ignore_date=True, payment_refund_sum=order.payment_refund_sum
            )
        except Quota.QuotaExceededException as e:
            _not_marked_paid(payment, e)


def _expired(row: BitcoinInvoice, payment: OrderPayment) -> bool:
    received = row.received
    open_states = (
        OrderPayment.PAYMENT_STATE_CREATED,
        OrderPayment.PAYMENT_STATE_PENDING,
    )
    if not received and payment.state in open_states:
        # Ended unpaid. No email: the customer saw the invoice end on its page, and pretix's
        # own reminders go on; the order's page offers to pay again.
        payment.fail(
            info=row.info(),
            log_data={"reason": "expired", "received": str(received)},
            send_mail=False,
        )
    if received:
        # Some bitcoin came, less than the amount: it reached the merchant's wallet, the order
        # is not paid. Not ended unpaid: paying again, by any method, would pay the order
        # twice. The payment stays pending for the organizer, who settles it with the
        # customer, then confirms it by hand or cancels it for the customer to pay again
        # (BitcoinInvoice.awaits_organizer).
        if payment.state in open_states:
            payment.info_data = row.info()
            payment.save(update_fields=["info"])
        payment.order.log_action(
            "pretix_sattleme.payment.underpaid",
            data={
                "local_id": payment.local_id,
                "received": str(received),
                "sats": str(row.sats),
            },
        )
        logger.warning(
            "Bitcoin invoice %s (payment %s) ended underpaid: %s of %s sats",
            row.ref,
            payment.full_id,
            received,
            row.sats,
        )
    return True


def _hold(order: Order, first: bool) -> None:
    """An on-chain payment came in time and is being confirmed (about 30 minutes, longer when
    its fee is low): keep its order from expiring meanwhile, which would free its products.

    An order pretix expired before a check saw the payment (its periodic tasks ran between the
    invoice's end and that check) is brought back, as pretix's backend does when its products
    are still available. Else it stays expired, and pretix decides once the payment is
    confirmed (`first`: the check that saw it first logs why).
    """
    if order.status == Order.STATUS_PENDING and order.expires >= now() + HOLD_MIN:
        return
    if order.status not in (Order.STATUS_PENDING, Order.STATUS_EXPIRED):
        return
    try:
        extend_order(order, now() + HOLD)
    except OrderError as e:
        if first:
            logger.warning(
                "Order %s expired, its Bitcoin payment being confirmed: not brought back (%s)",
                order.full_code,
                e,
            )


def next_delay(row: BitcoinInvoice, at: datetime | None = None) -> int:
    """Seconds until the next background check of the invoice."""
    at = now() if at is None else at
    seconds = int(at.timestamp())
    if seconds < row.end + END_MARGIN:
        # While it can be paid, whatever failed: every minute, once just before the end of its
        # grace period (a transaction a check lists by then came in time, SPEC.md §31) and
        # once just after (its final status).
        # Never sooner than WEB_INTERVAL: run_chain's check would skip itself (min_interval).
        marks = [m for m in (row.end - END_MARGIN, row.end + END_MARGIN) if m > seconds]
        return max(WEB_INTERVAL, min(OPEN_INTERVAL, marks[0] - seconds))
    errors = int(row.errors)
    if errors:
        return min(MAX_BACKOFF, OPEN_INTERVAL << min(errors - 1, 10))
    return SETTLING_INTERVAL


def workers() -> bool:
    """Whether Celery workers run the tasks: else pretix runs each task at once, in the
    caller, and a task scheduled for later would run right away."""
    from pretix.celery_app import app

    return not app.conf.task_always_eager


def _dispatch(
    event_id: int, invoice_id: int, chain: str | None = None, countdown: int = 0
) -> None:
    from .tasks import check_invoice

    check_invoice.apply_async(
        args=(event_id, invoice_id, chain),
        countdown=countdown,
        priority=getattr(settings, "PRIORITY_CELERY_LOW", None),
    )


def start_chain(
    row: BitcoinInvoice, delay: int | None = None, robust: bool = True
) -> None:
    """Start the background checks of an invoice, replacing those it had.

    `robust`: a task that cannot be dispatched is logged, not raised."""
    row.chain = uuid.uuid4().hex
    delay = next_delay(row) if delay is None else delay
    row.next_check = now() + timedelta(seconds=delay)
    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=row.pk).update(
            chain=row.chain, next_check=row.next_check
        )
    chain = row.chain
    if workers():
        event_id, invoice_id = row.payment.order.event_id, row.pk
        # Robust: a broker that cannot be reached must not fail what was committed (the
        # invoice made for a customer); the periodic tasks restart the chain.
        transaction.on_commit(
            lambda: _dispatch(event_id, invoice_id, chain, delay), robust=robust
        )


def run_chain(invoice_id: int, chain: str) -> None:
    """One step of the background checks of an invoice: check it, then schedule the next."""
    with scopes_disabled():
        current = BitcoinInvoice.objects.filter(pk=invoice_id, chain=chain, final=False)
        if not current.exists():
            return  # Final, or a newer chain checks it.
    check(invoice_id, min_interval=WEB_INTERVAL)
    with scopes_disabled():
        row = current.select_related("payment__order__event__organizer").first()
        if row is None:
            return
        if row.created < now() - GIVE_UP_AFTER:
            _give_up(row)
            return
        delay = next_delay(row)
        if not current.update(next_check=now() + timedelta(seconds=delay)):
            return
    if workers():
        _dispatch(row.payment.order.event_id, invoice_id, chain, delay)


def request_check(row: BitcoinInvoice) -> None:
    """Have the invoice checked soon, for its customer's page: at most every WEB_INTERVAL
    seconds. With Celery workers, in the background: the page reads the result later;
    without, now, within WEB_TIMEOUT."""
    at = now()
    if row.final:
        return
    if row.checked and row.checked > at - timedelta(seconds=WEB_INTERVAL):
        return
    if row.lease_until and row.lease_until > at:
        return
    if not workers():
        check(row.pk, min_interval=WEB_INTERVAL, timeout=WEB_TIMEOUT)
    elif cache.add(f"pretix_sattleme:check:{row.pk}", True, WEB_INTERVAL):
        _dispatch(row.payment.order.event_id, row.pk)


def sweep() -> None:
    """For pretix's periodic tasks: give up on invoices too old, and restart the background
    checks that stopped; without Celery workers, run the checks that are due."""
    at = now()
    with scopes_disabled():
        old = BitcoinInvoice.objects.filter(
            final=False, data__isnull=False, created__lt=at - GIVE_UP_AFTER
        ).select_related("payment__order__event__organizer")
        for row in old[:SWEEP_BATCH]:
            _give_up(row)

        with_workers = workers()
        due = (
            BitcoinInvoice.objects.filter(
                final=False,
                data__isnull=False,
                next_check__lte=at - STALE_CHAIN if with_workers else at,
            )
            .select_related("payment__order__event__organizer")
            .order_by("next_check")
        )
        started = time.monotonic()
        for row in due[: SWEEP_BATCH if with_workers else INLINE_BATCH]:
            if with_workers:
                # Not robust: a broker that cannot be reached stops the sweep at the first
                # invoice, not after a try for each one; the next sweep restarts them.
                start_chain(row, delay=0, robust=False)
                continue
            if time.monotonic() - started + WEB_TIMEOUT > INLINE_BUDGET:
                break  # The next run checks the others first: they are due the longest.
            check(row.pk, timeout=WEB_TIMEOUT)
            row.refresh_from_db()
            if not row.final:
                BitcoinInvoice.objects.filter(pk=row.pk).update(
                    next_check=now() + timedelta(seconds=next_delay(row))
                )


def _give_up(row: BitcoinInvoice) -> None:
    with scopes_disabled():
        if not BitcoinInvoice.objects.filter(pk=row.pk, final=False).update(
            final=True, error="abandoned"
        ):
            return
    shredding.finished(row)
    order = row.payment.order
    with scope(organizer=order.event.organizer):
        order.log_action(
            "pretix_sattleme.check.abandoned",
            data={
                "local_id": row.payment.local_id,
                "ref": row.ref,
                "days": GIVE_UP_AFTER.days,
            },
        )
    logger.warning(
        "Bitcoin invoice %s (payment %s): no final status after %s days, checks stopped",
        row.ref,
        row.payment.full_id,
        GIVE_UP_AFTER.days,
    )
