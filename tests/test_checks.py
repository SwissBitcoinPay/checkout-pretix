from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from django.core import mail
from django.utils.timezone import now
from django_scopes import scopes_disabled
from pretix.base.models import Order, OrderPayment, Quota
from pretix.base.services.orders import expire_orders
from sattleme_checkout import (
    ChannelNotFoundError,
    InvalidInvoiceError,
    InvalidOrderError,
    NetworkError,
    PaymentStatus,
    Rest,
    RevokedError,
)

from pretix_sattleme import checks, sdk
from pretix_sattleme.models import BitcoinInvoice

from .conftest import make_invoice

PREIMAGE = "cd" * 32
TXID = "ef" * 32


def reload(row):
    with scopes_disabled():
        return BitcoinInvoice.objects.select_related("payment__order").get(pk=row.pk)


def logs(order, action):
    with scopes_disabled():
        return list(order.all_logentries().filter(action_type=action))


@pytest.mark.django_db
def test_paid_confirms_the_payment(fake_sdk, invoice_row, order):
    fake_sdk.statuses = [PaymentStatus.paid_lightning(PREIMAGE)]
    assert checks.check(invoice_row.pk)

    row = reload(invoice_row)
    assert row.final and row.state == "paid" and row.lease_until is None
    assert fake_sdk.checked[0] == (row.data, sdk.CHECK_TIMEOUT)
    payment = row.payment
    assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
    assert payment.info_data["status"]["preimage"] == PREIMAGE
    order.refresh_from_db()
    assert order.status == Order.STATUS_PAID
    assert any("ABC12" in m.subject for m in mail.outbox)

    # Final: never checked again.
    assert not checks.check(invoice_row.pk)
    assert len(fake_sdk.checked) == 1


@pytest.mark.parametrize(
    "status",
    [
        PaymentStatus.paid_lightning(PREIMAGE),
        PaymentStatus.paid_onchain(49_000, (TXID,)),
        PaymentStatus.paid_mixed(49_000, (TXID,), (PREIMAGE,)),
    ],
    ids=["lightning", "onchain", "mixed"],
)
@pytest.mark.django_db
def test_paid_by_each_method_confirms_the_payment(fake_sdk, invoice_row, order, status):
    fake_sdk.statuses = [status]
    assert checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert row.final and row.state == "paid"
    assert row.payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
    # What the SDK reported, kept in the payment's information (its proof of payment, its
    # transactions).
    assert row.payment.info_data["status"] == status.to_json()
    order.refresh_from_db()
    assert order.status == Order.STATUS_PAID


@pytest.mark.django_db
def test_paid_after_the_payment_was_canceled(fake_sdk, invoice_row, order):
    """The plugin never cancels a payment whose invoice may be paid (test_changed_orders.py);
    should one be canceled all the same, its bitcoin came: confirmed, as the order still owes
    the invoice's price."""
    with scopes_disabled():
        OrderPayment.objects.filter(pk=invoice_row.payment_id).update(state="canceled")
    fake_sdk.statuses = [PaymentStatus.paid_onchain(49_000, (TXID,))]
    checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert row.final
    assert row.payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
    order.refresh_from_db()
    assert order.status == Order.STATUS_PAID


@pytest.mark.django_db
def test_paid_late_for_a_sold_out_order(fake_sdk, invoice_row, order, event, item):
    order.status = Order.STATUS_EXPIRED
    order.save()
    event.settings.set("payment_term_accept_late", True)
    with scopes_disabled():
        Quota.objects.filter(event=event).update(size=0)
    fake_sdk.statuses = [PaymentStatus.paid_lightning(PREIMAGE)]
    checks.check(invoice_row.pk)

    row = reload(invoice_row)
    # pretix keeps the payment and leaves the order to the organizer: nothing more to check.
    assert row.final
    assert row.payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
    order.refresh_from_db()
    assert order.status == Order.STATUS_EXPIRED
    # The order's history says why.
    [entry] = logs(order, "pretix.event.order.quotaexceeded")
    assert "no longer available" in entry.parsed_data["message"]


@pytest.mark.django_db
def test_paid_in_time_counts_after_the_last_date_of_payments(
    fake_sdk, invoice_row, order, monkeypatch
):
    # Its confirmations ended after pretix's deadline; the invoice ended before it.
    monkeypatch.setattr(
        type(order),
        "payment_term_last",
        property(lambda self: now() - timedelta(hours=1)),
    )
    fake_sdk.statuses = [PaymentStatus.paid_onchain(49_000, (TXID,))]
    checks.check(invoice_row.pk)
    order.refresh_from_db()
    assert order.status == Order.STATUS_PAID


@pytest.mark.django_db
def test_paid_in_time_counts_for_an_order_expired_meanwhile(
    fake_sdk, invoice_row, order, event
):
    event.settings.set("payment_term_accept_late", False)
    order.status = Order.STATUS_EXPIRED
    order.save()
    fake_sdk.statuses = [PaymentStatus.paid_onchain(49_000, (TXID,))]
    checks.check(invoice_row.pk)
    order.refresh_from_db()
    assert order.status == Order.STATUS_PAID


@pytest.mark.django_db
def test_expired_fails_the_payment_without_email(fake_sdk, invoice_row, order):
    fake_sdk.statuses = [PaymentStatus.expired(0)]
    checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert row.final and row.state == "expired"
    assert row.payment.state == OrderPayment.PAYMENT_STATE_FAILED
    assert mail.outbox == []
    assert logs(order, "pretix_sattleme.payment.underpaid") == []
    order.refresh_from_db()
    assert order.status == Order.STATUS_PENDING


@pytest.mark.django_db
def test_expired_underpaid_is_logged_and_left_to_the_organizer(
    fake_sdk, invoice_row, order
):
    """Not ended unpaid: what came reached the merchant's wallet. The payment stays pending
    (test_one_payable_invoice.py), with what the check found."""
    fake_sdk.statuses = [PaymentStatus.expired(30_000)]
    checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert row.final and row.awaits_organizer(row.payment)
    assert row.payment.state == OrderPayment.PAYMENT_STATE_PENDING
    assert row.payment.info_data["status"] == {"state": "expired", "received": "30000"}
    [entry] = logs(order, "pretix_sattleme.payment.underpaid")
    assert entry.parsed_data == {"local_id": 1, "received": "30000", "sats": "49000"}
    # Its amounts grouped, as the payment's details show them.
    assert "30,000 of 49,000 sats" in str(entry.display())


@pytest.mark.django_db
def test_expired_keeps_a_canceled_payment_canceled(fake_sdk, invoice_row):
    with scopes_disabled():
        OrderPayment.objects.filter(pk=invoice_row.payment_id).update(state="canceled")
    fake_sdk.statuses = [PaymentStatus.expired(0)]
    checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert row.final
    assert row.payment.state == OrderPayment.PAYMENT_STATE_CANCELED


@pytest.mark.django_db
def test_unconfirmed_waits_and_is_logged_once(fake_sdk, invoice_row, order):
    fake_sdk.statuses = [
        PaymentStatus.unconfirmed(49_000, 1, (TXID,)),
        PaymentStatus.unconfirmed(49_000, 2, (TXID,)),
    ]
    checks.check(invoice_row.pk)
    checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert not row.final and row.state == "unconfirmed" and row.confirmations == 2
    assert row.payment.state == OrderPayment.PAYMENT_STATE_PENDING
    assert len(logs(order, "pretix_sattleme.payment.unconfirmed")) == 1


@pytest.mark.django_db
def test_an_underpayment_the_merchant_accepted_completes_the_order(
    fake_sdk, payment, order
):
    """The merchant's acceptance (SPEC.md §31): in the sattle.me app, the merchant accepted
    what an order underpaid on-chain received, short by more than any tolerance. The SDK
    reports it unconfirmed, then paid, with `received` below the invoice's sats, as with the
    tolerance: held from expiring while confirmed, then paid, never left to the organizer
    for the difference alone, which is noted on the order."""
    invoice = make_invoice(onchain=True)
    with scopes_disabled():
        row = BitcoinInvoice.objects.create(
            payment=payment, ref=invoice.ref, data=invoice.to_json(), next_check=now()
        )
        payment.state = OrderPayment.PAYMENT_STATE_PENDING
        payment.save(update_fields=["state"])
    fake_sdk.statuses = [
        PaymentStatus.unconfirmed(20_000, 1, (TXID,)),
        PaymentStatus.paid_onchain(20_000, (TXID,)),
    ]
    checks.check(row.pk)
    row = reload(row)
    assert not row.final and row.state == "unconfirmed" and row.received == 20_000
    assert row.phase(row.payment) == "unconfirmed" and row.may_be_paid()
    assert row.payment.state == OrderPayment.PAYMENT_STATE_PENDING
    order.refresh_from_db()
    assert order.expires > now() + checks.HOLD_MIN
    assert logs(order, "pretix_sattleme.payment.underpaid") == []

    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=row.pk).update(checked=None)
    assert checks.check(row.pk)
    row = reload(row)
    assert row.final and row.state == "paid" and row.error == ""
    assert row.payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
    assert row.payment.info_data["status"]["received"] == "20000"
    order.refresh_from_db()
    assert order.status == Order.STATUS_PAID
    assert logs(order, "pretix_sattleme.payment.changed") == []
    assert logs(order, "pretix_sattleme.payment.underpaid") == []
    [note] = logs(order, "pretix_sattleme.payment.amount")
    assert note.parsed_data == {"local_id": 1, "received": "20000", "sats": "49000"}
    text = str(note.display())
    assert "with 20,000 sats" in text and "merchant accepted" in text


@pytest.mark.django_db
def test_open_status_is_stored(fake_sdk, invoice_row):
    rest = Rest(
        sats=19_000, bolt11="lnbc190u1rest", payment_uri="lightning:lnbc190u1rest"
    )
    fake_sdk.statuses = [PaymentStatus.open(invoice_row.expires_at, 30_000, rest)]
    checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert not row.final and row.checked is not None
    assert row.received == 30_000 and row.rest["bolt11"] == "lnbc190u1rest"


@pytest.mark.django_db
def test_failures_are_counted_and_logged_once(fake_sdk, invoice_row, caplog):
    fake_sdk.statuses = [
        NetworkError("no relay"),
        NetworkError("no relay"),
        RevokedError("gone"),
    ]
    for _ in range(3):
        checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert (row.error, row.errors) == ("revoked", 3)
    assert row.lease_until is None and not row.final
    messages = [r.getMessage() for r in caplog.records if "failed" in r.getMessage()]
    assert len(messages) == 2
    assert "network: no relay" in messages[0] and "revoked: gone" in messages[1]

    fake_sdk.statuses = [PaymentStatus.open(row.expires_at, 0, None)]
    checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert (row.error, row.errors) == ("", 0)


@pytest.mark.parametrize(
    "error, code",
    [
        (NetworkError("no relay"), "network"),
        (TimeoutError(), "timeout"),
        (RevokedError("removed"), "revoked"),
        (ChannelNotFoundError("no channel", newer_format=False), "channel_not_found"),
        (ChannelNotFoundError("newer", newer_format=True), "channel_not_found"),
        (InvalidOrderError("invoice: not valid", param="invoice"), "invalid_order"),
        (InvalidInvoiceError("rest: wrong amount", check="amount"), "invalid_invoice"),
        (RuntimeError("a bug"), "error"),
    ],
)
@pytest.mark.django_db
def test_every_error_of_a_check_is_kept_and_checked_again(
    fake_sdk, invoice_row, order, error, code
):
    """Whether the SDK says the same call may succeed later or not, the payment may have
    come: its invoice is checked again (less often: next_delay), never failed for it."""
    fake_sdk.statuses = [error]
    assert checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert (row.error, row.errors) == (code, 1)
    assert not row.final and row.lease_until is None and row.checked is not None
    assert row.payment.state == OrderPayment.PAYMENT_STATE_PENDING
    order.refresh_from_db()
    assert order.status == Order.STATUS_PENDING

    fake_sdk.statuses = [PaymentStatus.paid_lightning(PREIMAGE)]
    assert checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert row.final and (row.error, row.errors) == ("", 0)
    assert row.payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED


@pytest.mark.django_db
def test_a_check_with_a_key_no_longer_valid(fake_sdk, invoice_row, event):
    """The real SDK refuses the key (InvalidKeyError) before any request."""
    event.settings.set(sdk.KEY_SETTING, "sattleme_ck_nope")
    assert checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert (row.error, row.errors) == ("invalid_key", 1) and not row.final
    assert fake_sdk.checked == []


@pytest.mark.django_db
def test_one_check_at_a_time(fake_sdk, invoice_row):
    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(
            lease_until=now() + timedelta(seconds=30)
        )
    assert not checks.check(invoice_row.pk)
    assert fake_sdk.checked == []

    # A lease of a check that died runs out.
    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(
            lease_until=now() - timedelta(seconds=1)
        )
    assert checks.check(invoice_row.pk)


@pytest.mark.django_db
def test_a_check_that_outlived_its_lease_writes_nothing(
    fake_sdk, invoice_row, monkeypatch
):
    """A check slower than its lease: another check holds the invoice now."""
    other = now() + timedelta(minutes=5)

    def slow_status(self, invoice, timeout=None):
        with scopes_disabled():
            BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(lease_until=other)
        return PaymentStatus.paid_lightning(PREIMAGE)

    from .conftest import FakeClient

    monkeypatch.setattr(FakeClient, "get_status", slow_status)
    assert checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert not row.final and row.status is None and row.lease_until == other
    assert row.payment.state == OrderPayment.PAYMENT_STATE_PENDING


@pytest.mark.django_db
def test_min_interval(fake_sdk, invoice_row):
    assert checks.check(invoice_row.pk, min_interval=60)
    assert not checks.check(invoice_row.pk, min_interval=60)
    assert checks.check(invoice_row.pk)
    assert len(fake_sdk.checked) == 2


@pytest.mark.django_db
def test_a_failure_to_apply_a_status_is_retried(fake_sdk, invoice_row, monkeypatch):
    def broken(self, *args, **kwargs):
        raise RuntimeError("database gone")

    confirm = OrderPayment.confirm
    monkeypatch.setattr(OrderPayment, "confirm", broken)
    fake_sdk.statuses = [PaymentStatus.paid_lightning(PREIMAGE)]
    checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert not row.final and row.lease_until is None and row.state == "paid"

    # Failures in a row are counted, as those of the SDK: the checks back off.
    for errors in (1, 2):
        assert (row.error, row.errors) == ("apply", errors)
        fake_sdk.statuses = [PaymentStatus.paid_lightning(PREIMAGE)]
        checks.check(invoice_row.pk)
        row = reload(invoice_row)

    monkeypatch.setattr(OrderPayment, "confirm", confirm)
    fake_sdk.statuses = [PaymentStatus.paid_lightning(PREIMAGE)]
    checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert row.final and (row.error, row.errors) == ("", 0)
    assert row.payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED


@pytest.mark.django_db
def test_a_check_keeps_an_invoice_given_up_meanwhile(
    fake_sdk, invoice_row, monkeypatch
):
    """Given up on by the periodic tasks while a check applies its status: it stays so."""
    paid = checks._paid

    def given_up_meanwhile(row, payment):
        with scopes_disabled():
            BitcoinInvoice.objects.filter(pk=row.pk).update(
                final=True, error="abandoned"
            )
        return paid(row, payment)

    monkeypatch.setattr(checks, "_paid", given_up_meanwhile)
    fake_sdk.statuses = [PaymentStatus.paid_lightning(PREIMAGE)]
    checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert row.final and row.error == "abandoned" and row.lease_until is None


@pytest.mark.django_db
def test_next_delay(invoice_row):
    at = now()
    seconds = int(at.timestamp())
    row = invoice_row
    row.data = {**row.data, "expiresAt": seconds + 600}
    assert checks.next_delay(row, at) == checks.OPEN_INTERVAL
    # Once just before the end of the grace period (end = expiresAt + 120), once just after.
    row.data = {**row.data, "expiresAt": seconds - 80}
    assert checks.next_delay(row, at) == 30
    row.data = {**row.data, "expiresAt": seconds - 115}
    assert checks.next_delay(row, at) == 15
    # Never sooner than a chain's check may run (min_interval), which would skip it: still
    # before the end.
    row.data = {**row.data, "expiresAt": seconds - 108}
    assert checks.next_delay(row, at) == checks.WEB_INTERVAL
    assert checks.END_MARGIN > checks.WEB_INTERVAL
    # Failures do not delay the checks while it can be paid.
    row.errors = 5
    row.data = {**row.data, "expiresAt": seconds + 600}
    assert checks.next_delay(row, at) == checks.OPEN_INTERVAL
    row.errors = 0
    row.data = {**row.data, "expiresAt": seconds - 200}
    assert checks.next_delay(row, at) == checks.SETTLING_INTERVAL
    row.errors = 1
    assert checks.next_delay(row, at) == 60
    row.errors = 3
    assert checks.next_delay(row, at) == 240
    row.errors = 50
    assert checks.next_delay(row, at) == checks.MAX_BACKOFF


class Dispatched(list[tuple[Any, ...]]):
    def __call__(self, event_id, invoice_id, chain=None, countdown=0):
        self.append((event_id, invoice_id, chain, countdown))


@pytest.fixture
def with_workers(monkeypatch):
    dispatched = Dispatched()
    monkeypatch.setattr(checks, "workers", lambda: True)
    monkeypatch.setattr(checks, "_dispatch", dispatched)
    return dispatched


@pytest.mark.django_db
def test_chain(
    fake_sdk, invoice_row, event, with_workers, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks(execute=True):
        checks.start_chain(invoice_row)
    chain = reload(invoice_row).chain
    assert with_workers == [(event.pk, invoice_row.pk, chain, checks.OPEN_INTERVAL)]

    checks.run_chain(invoice_row.pk, chain)
    assert len(fake_sdk.checked) == 1
    assert with_workers[-1] == (event.pk, invoice_row.pk, chain, checks.OPEN_INTERVAL)

    # A chain that was replaced stops.
    checks.run_chain(invoice_row.pk, "0" * 32)
    assert len(fake_sdk.checked) == 1 and len(with_workers) == 2

    # The chain of a final invoice stops.
    fake_sdk.statuses = [PaymentStatus.paid_lightning(PREIMAGE)]
    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(checked=None)
    checks.run_chain(invoice_row.pk, chain)
    assert reload(invoice_row).final and len(with_workers) == 2


@pytest.mark.django_db
def test_chain_gives_up(fake_sdk, invoice_row, order, with_workers):
    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(
            created=now() - checks.GIVE_UP_AFTER - timedelta(days=1), chain="c" * 32
        )
    checks.run_chain(invoice_row.pk, "c" * 32)
    row = reload(invoice_row)
    assert row.final and row.error == "abandoned"
    assert with_workers == []
    # Its final status unknown: the payment stays pending for the organizer, the order keeps
    # the invoice as its own.
    assert row.payment.state == OrderPayment.PAYMENT_STATE_PENDING
    assert row.awaits_organizer(row.payment) and row.may_be_paid()
    # After three months: a payment made in time may take that long to be confirmed.
    [entry] = logs(order, "pretix_sattleme.check.abandoned")
    assert entry.parsed_data["days"] == 90
    # Named by its payment, not by its reference, too long for the order's history.
    assert str(entry.display()).startswith(
        "The Bitcoin invoice of payment 1 had no final status after 90 days"
    )


@pytest.mark.django_db
def test_sweep_without_workers_runs_due_checks(fake_sdk, invoice_row):
    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(
            next_check=now() + timedelta(minutes=1)
        )
    checks.sweep()
    assert fake_sdk.checked == []

    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(next_check=now())
    checks.sweep()
    assert len(fake_sdk.checked) == 1
    assert reload(invoice_row).next_check > now()


@pytest.mark.django_db
def test_sweep_without_workers_ends_in_time(fake_sdk, invoice_row, order, monkeypatch):
    """The checks run inline end within INLINE_BUDGET; the next run checks the others."""
    with scopes_disabled():
        other = order.payments.create(
            provider="sattleme", state="pending", amount=order.total
        )
        BitcoinInvoice.objects.create(
            payment=other,
            ref="pretix-ABC12-2-other",
            data=make_invoice(ref="pretix-ABC12-2-other").to_json(),
            next_check=now(),
        )
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(
            next_check=now() - timedelta(minutes=1)
        )
    # Each check takes as long as it may, and there is room for one.
    clock = [0]
    check = checks.check

    def slow_check(*args, **kwargs):
        clock[0] += checks.WEB_TIMEOUT
        return check(*args, **kwargs)

    monkeypatch.setattr(checks, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(checks, "check", slow_check)
    monkeypatch.setattr(checks, "INLINE_BUDGET", checks.WEB_TIMEOUT + 1)
    checks.sweep()
    assert [invoice["ref"] for invoice, _ in fake_sdk.checked] == [invoice_row.ref]
    checks.sweep()
    assert [invoice["ref"] for invoice, _ in fake_sdk.checked] == [
        invoice_row.ref,
        "pretix-ABC12-2-other",
    ]


@pytest.mark.django_db
def test_sweep_with_workers_restarts_stopped_chains(
    fake_sdk, invoice_row, with_workers, django_capture_on_commit_callbacks
):
    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(
            next_check=now() - timedelta(minutes=5), chain="a" * 32
        )
    checks.sweep()
    assert with_workers == []  # Late, but not stopped.

    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(
            next_check=now() - checks.STALE_CHAIN - timedelta(minutes=1)
        )
    with django_capture_on_commit_callbacks(execute=True):
        checks.sweep()
    [(_, invoice_id, chain, countdown)] = with_workers
    assert invoice_id == invoice_row.pk and countdown == 0 and chain != "a" * 32
    assert reload(invoice_row).chain == chain
    assert fake_sdk.checked == []


@pytest.mark.django_db
def test_sweep_gives_up_on_old_invoices(fake_sdk, invoice_row):
    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(
            created=now() - checks.GIVE_UP_AFTER - timedelta(days=1)
        )
    checks.sweep()
    assert reload(invoice_row).final
    assert fake_sdk.checked == []


@pytest.mark.django_db
def test_request_check(fake_sdk, invoice_row, with_workers, monkeypatch):
    checks.request_check(invoice_row)
    assert with_workers == [
        (invoice_row.payment.order.event_id, invoice_row.pk, None, 0)
    ]

    invoice_row.checked = now()
    checks.request_check(invoice_row)
    assert len(with_workers) == 1

    monkeypatch.setattr(checks, "workers", lambda: False)
    invoice_row.checked = None
    checks.request_check(invoice_row)
    assert len(fake_sdk.checked) == 1


@pytest.mark.django_db
def test_task(fake_sdk, invoice_row, event):
    from pretix_sattleme.tasks import check_invoice

    check_invoice.apply(args=(event.pk, invoice_row.pk))
    assert len(fake_sdk.checked) == 1


@pytest.mark.django_db
def test_a_confirmed_payment_of_an_unpaid_order_is_finished(
    fake_sdk, invoice_row, order, monkeypatch
):
    """pretix confirmed the payment, then failed to mark the order paid (a plugin's receiver
    raised): a later check marks it paid."""
    from pretix.base.models import OrderPayment as Payment

    mark = Payment._mark_order_paid

    def broken(self, *args, **kwargs):
        raise RuntimeError("a plugin failed")

    monkeypatch.setattr(Payment, "_mark_order_paid", broken)
    fake_sdk.statuses = [PaymentStatus.paid_lightning(PREIMAGE)]
    checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert not row.final and row.error == "apply"
    assert row.payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
    order.refresh_from_db()
    assert order.status == Order.STATUS_PENDING

    monkeypatch.setattr(Payment, "_mark_order_paid", mark)
    fake_sdk.statuses = [PaymentStatus.paid_lightning(PREIMAGE)]
    checks.check(invoice_row.pk)
    assert reload(invoice_row).final
    order.refresh_from_db()
    assert order.status == Order.STATUS_PAID


@pytest.mark.django_db
def test_a_confirmed_payment_of_a_sold_out_order_is_logged(
    fake_sdk, invoice_row, order, event
):
    """A confirmed payment whose expired order can no longer be marked paid: the order's
    history says why, and nothing more is checked."""
    with scopes_disabled():
        OrderPayment.objects.filter(pk=invoice_row.payment_id).update(state="confirmed")
        Quota.objects.filter(event=event).update(size=0)
    order.status = Order.STATUS_EXPIRED
    order.save()
    fake_sdk.statuses = [PaymentStatus.paid_lightning(PREIMAGE)]
    checks.check(invoice_row.pk)
    assert reload(invoice_row).final
    order.refresh_from_db()
    assert order.status == Order.STATUS_EXPIRED
    [entry] = logs(order, "pretix.event.order.quotaexceeded")
    assert "no longer available" in entry.parsed_data["message"]


@pytest.mark.django_db
def test_a_chain_that_cannot_be_dispatched(
    fake_sdk, invoice_row, monkeypatch, django_capture_on_commit_callbacks
):
    """A broker that cannot be reached: what was committed stands (the invoice made for a
    customer); a sweep stops at the first invoice, the next one restarts the chains."""

    def unreachable(*args, **kwargs):
        raise ConnectionError("no broker")

    monkeypatch.setattr(checks, "workers", lambda: True)
    monkeypatch.setattr(checks, "_dispatch", unreachable)
    with django_capture_on_commit_callbacks(execute=True):
        checks.start_chain(invoice_row)
    assert reload(invoice_row).chain == invoice_row.chain

    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(
            next_check=now() - checks.STALE_CHAIN - timedelta(minutes=1)
        )
    with pytest.raises(ConnectionError):
        with django_capture_on_commit_callbacks(execute=True):
            checks.sweep()


@pytest.mark.django_db
def test_unconfirmed_keeps_the_order_from_expiring(fake_sdk, invoice_row, order):
    order.expires = now() + timedelta(minutes=10)
    order.save()
    fake_sdk.statuses = [PaymentStatus.unconfirmed(49_000, 1, (TXID,))]
    checks.check(invoice_row.pk)
    order.refresh_from_db()
    assert order.expires > now() + checks.HOLD_MIN
    with scopes_disabled():
        assert (
            order.all_logentries()
            .filter(action_type="pretix.event.order.expirychanged")
            .exists()
        )

    # Far enough already: unchanged.
    expires = order.expires
    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(checked=None)
    fake_sdk.statuses = [PaymentStatus.unconfirmed(49_000, 2, (TXID,))]
    checks.check(invoice_row.pk)
    order.refresh_from_db()
    assert order.expires == expires


@pytest.mark.django_db
def test_an_order_expired_before_the_check_that_saw_its_payment_is_held(
    fake_sdk, invoice_row, order, event
):
    """pretix's periodic tasks expired the order between the invoice's end and the check
    that saw its on-chain payment, made in time: brought back, as its products are still
    available, and held until the payment is confirmed."""
    event.settings.set("payment_term_expire_automatically", True)
    with scopes_disabled():
        Order.objects.filter(pk=order.pk).update(expires=now() - timedelta(seconds=1))
        expire_orders(None)
        order.refresh_from_db()
    assert order.status == Order.STATUS_EXPIRED
    fake_sdk.statuses = [PaymentStatus.unconfirmed(49_000, 0, (TXID,))]
    checks.check(invoice_row.pk)
    order.refresh_from_db()
    assert order.status == Order.STATUS_PENDING
    assert order.expires > now() + checks.HOLD_MIN
    assert reload(invoice_row).error == ""

    fake_sdk.statuses = [PaymentStatus.paid_onchain(49_000, (TXID,))]
    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(checked=None)
    checks.check(invoice_row.pk)
    order.refresh_from_db()
    assert order.status == Order.STATUS_PAID


@pytest.mark.django_db
def test_an_expired_order_whose_products_are_gone_stays_expired(
    fake_sdk, invoice_row, order, event, caplog
):
    """Its products sold meanwhile: pretix keeps it expired, and decides once the payment is
    confirmed (test_paid_late_for_a_sold_out_order). The check itself succeeded."""
    with scopes_disabled():
        Order.objects.filter(pk=order.pk).update(
            status=Order.STATUS_EXPIRED, expires=now() - timedelta(minutes=5)
        )
        Quota.objects.filter(event=event).update(size=0)
    fake_sdk.statuses = [
        PaymentStatus.unconfirmed(49_000, 0, (TXID,)),
        PaymentStatus.unconfirmed(49_000, 1, (TXID,)),
    ]
    checks.check(invoice_row.pk)
    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(checked=None)
    checks.check(invoice_row.pk)
    order.refresh_from_db()
    assert order.status == Order.STATUS_EXPIRED
    row = reload(invoice_row)
    assert row.error == "" and row.confirmations == 1
    held = [r for r in caplog.records if "not brought back" in r.getMessage()]
    assert len(held) == 1


@pytest.mark.django_db
def test_paid_when_already_confirmed(fake_sdk, invoice_row, order, monkeypatch):
    with scopes_disabled():
        OrderPayment.objects.filter(pk=invoice_row.payment_id).update(state="confirmed")
    confirmed = []
    monkeypatch.setattr(
        OrderPayment, "confirm", lambda self, **kw: confirmed.append(self)
    )
    fake_sdk.statuses = [PaymentStatus.paid_lightning(PREIMAGE)]
    checks.check(invoice_row.pk)
    assert reload(invoice_row).final and confirmed == []


@pytest.mark.django_db
def test_request_check_skips_what_needs_no_check(fake_sdk, invoice_row, with_workers):
    invoice_row.final = True
    checks.request_check(invoice_row)
    invoice_row.final = False
    invoice_row.lease_until = now() + timedelta(seconds=30)
    checks.request_check(invoice_row)
    assert with_workers == [] and fake_sdk.checked == []


@pytest.mark.django_db
def test_dispatch_matches_the_task(fake_sdk, invoice_row, event, monkeypatch, settings):
    from pretix_sattleme.tasks import check_invoice

    sent = []
    monkeypatch.setattr(
        check_invoice, "apply_async", lambda **kwargs: sent.append(kwargs)
    )
    settings.PRIORITY_CELERY_LOW = 9
    checks._dispatch(event.pk, invoice_row.pk, "c" * 32, 60)
    assert sent == [
        {"args": (event.pk, invoice_row.pk, "c" * 32), "countdown": 60, "priority": 9}
    ]
    # What is sent runs the task: one step of the chain.
    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(chain="c" * 32)
    check_invoice.apply(args=sent[0]["args"])
    assert len(fake_sdk.checked) == 1


@pytest.mark.django_db
def test_periodic_task_sweeps(fake_sdk, invoice_row):
    from pretix.base.signals import periodic_task

    periodic_task.send(None)
    assert len(fake_sdk.checked) == 1
