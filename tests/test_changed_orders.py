"""An order whose amount due changed after its invoice was made (the organizer changed it, in
pretix's backend or its REST API, or the customer did when the event allows it; the order was
canceled, or paid otherwise), then the invoice paid: never completed automatically. pretix has no
state of an order for the organizer to look at: the payment stays pending, with a note of the
amount received and what the order owes now, and the organizer confirms it by hand.

From the audit before the first release (its proofs: test_order_changed.py), through pretix's own
code: its OrderChangeManager, cancel_order and pages."""

from decimal import Decimal

import pytest
from django.core import mail
from django_scopes import scopes_disabled
from pretix.base.models import Order, OrderPayment, OrderRefund
from pretix.base.payment import PaymentException
from pretix.base.services.orders import OrderChangeManager, cancel_order
from sattleme_checkout import PaymentStatus, Price

from pretix_sattleme import checks, display, invoices, links
from pretix_sattleme.models import BitcoinInvoice

from .conftest import make_invoice

PREIMAGE = "cd" * 32
TXID = "ef" * 32


def change_total(order, price):
    """The order's only position at another price, as pretix's backend changes it."""
    with scopes_disabled():
        order = Order.objects.get(pk=order.pk)
        ocm = OrderChangeManager(order, notify=False)
        ocm.change_price(order.positions.first(), Decimal(price))
        ocm.commit()
        order.refresh_from_db()
        return order


def reload(row):
    with scopes_disabled():
        return BitcoinInvoice.objects.select_related("payment__order").get(pk=row.pk)


def entries(order, action):
    with scopes_disabled():
        return list(order.all_logentries().filter(action_type=action))


def enable_bank_transfer(event):
    with scopes_disabled():
        event.plugins = "pretix_sattleme,pretix.plugins.banktransfer"
        event.save()
        # A plugin of both levels on pretix 2026.8: enabled for the organizer too.
        event.organizer.plugins = "pretix.plugins.banktransfer"
        event.organizer.save()
        event.settings.set("payment_banktransfer__enabled", True)


def pay_change(client, order):
    return client.get(f"/org/conf/order/{order.code}/{order.secret}/pay/change")


@pytest.mark.django_db
def test_an_order_lowered_after_its_invoice_is_never_completed_automatically(
    fake_sdk, invoice_row, order, payment
):
    order = change_total(order, "20.00")
    assert order.total == Decimal("20.00")
    with scopes_disabled():
        payment.refresh_from_db()
    # pretix asked to cancel the payment: refused, its invoice (24.50 CHF) may be paid.
    assert payment.state == OrderPayment.PAYMENT_STATE_PENDING
    [failed] = entries(order, "pretix.event.order.payment.canceled.failed")
    assert "may still be paid" in failed.parsed_data["error"]

    mail.outbox = []
    fake_sdk.statuses = [PaymentStatus.paid_lightning(PREIMAGE)]
    assert checks.check(invoice_row.pk)
    row = reload(invoice_row)
    order.refresh_from_db()
    # Never completed automatically: the payment stays pending for the organizer.
    assert order.status == Order.STATUS_PENDING
    assert row.final and row.state == "paid" and row.error == "changed"
    assert row.payment.state == OrderPayment.PAYMENT_STATE_PENDING
    assert row.payment.info_data["status"]["preimage"] == PREIMAGE
    assert entries(order, "pretix.event.order.paid") == []
    assert mail.outbox == []
    [note] = entries(order, "pretix_sattleme.payment.changed")
    assert note.parsed_data == {
        "local_id": 1,
        "amount": "24.50",
        "currency": "CHF",
        "received": "49000",
        "owed": "20.00",
    }
    # Final: never checked again.
    assert not checks.check(invoice_row.pk)


@pytest.mark.django_db
def test_an_order_raised_after_its_invoice_gets_a_note_with_both_amounts(
    fake_sdk, invoice_row, order, payment
):
    order = change_total(order, "30.00")
    fake_sdk.statuses = [PaymentStatus.paid_lightning(PREIMAGE)]
    assert checks.check(invoice_row.pk)
    order.refresh_from_db()
    assert order.status == Order.STATUS_PENDING
    [note] = entries(order, "pretix_sattleme.payment.changed")
    assert note.parsed_data["amount"] == "24.50" and note.parsed_data["owed"] == "30.00"
    text = str(note.display())
    assert "CHF24.50 (49,000 sats)" in text and "owes CHF30.00 now" in text
    assert "confirm it by hand" in text


@pytest.mark.django_db
def test_no_other_method_while_the_invoice_of_a_changed_order_may_be_paid(
    client, fake_sdk, invoice_row, order, payment, event
):
    """One payable invoice per order: after the organizer changed the order, its Bitcoin
    invoice (payable for another 15 minutes) is still the order's, its payment pending.
    """
    enable_bank_transfer(event)
    order = change_total(order, "30.00")
    assert reload(invoice_row).may_be_paid()
    page = pay_change(client, order)
    offered = (
        page.status_code == 200 and 'value="banktransfer"' in page.content.decode()
    )
    assert not offered


@pytest.mark.django_db
def test_a_canceled_order_paid_afterwards_is_left_to_the_organizer(
    fake_sdk, invoice_row, order, payment
):
    with scopes_disabled():
        cancel_order(order.pk, send_mail=False)
        order.refresh_from_db()
        payment.refresh_from_db()
    assert order.status == Order.STATUS_CANCELED
    assert payment.state == OrderPayment.PAYMENT_STATE_PENDING
    fake_sdk.statuses = [PaymentStatus.paid_onchain(49_000, (TXID,))]
    assert checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert row.final and row.payment.state == OrderPayment.PAYMENT_STATE_PENDING
    [note] = entries(order, "pretix_sattleme.payment.changed")
    assert note.parsed_data["owed"] == "0.00"


@pytest.mark.django_db
def test_an_order_paid_otherwise_meanwhile_is_left_to_the_organizer(
    fake_sdk, invoice_row, order, payment
):
    with scopes_disabled():
        order.payments.create(
            provider="manual", amount=Decimal("24.50"), state="created"
        ).confirm()
        order.refresh_from_db()
    assert order.status == Order.STATUS_PAID
    fake_sdk.statuses = [PaymentStatus.paid_lightning(PREIMAGE)]
    assert checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert row.final and row.payment.state == OrderPayment.PAYMENT_STATE_PENDING
    [note] = entries(order, "pretix_sattleme.payment.changed")
    assert note.parsed_data["owed"] == "0.00"


@pytest.mark.django_db
def test_what_the_order_owes_counts_its_other_payments_and_refunds(
    fake_sdk, order, payment
):
    """A gift card paid 10.00 of the order, refunded 5.00 since: the order owes 19.50, the
    invoice made for it is of 19.50, and its payment completes the order."""
    with scopes_disabled():
        other = order.payments.create(
            provider="giftcard", amount=Decimal("10.00"), state="confirmed"
        )
        order.refunds.create(
            payment=other,
            provider="giftcard",
            amount=Decimal("5.00"),
            state=OrderRefund.REFUND_STATE_DONE,
            source=OrderRefund.REFUND_SOURCE_ADMIN,
        )
        assert order.pending_sum == Decimal("19.50")
        OrderPayment.objects.filter(pk=payment.pk).update(
            amount=Decimal("19.50"), state="pending"
        )
        invoice = make_invoice(fiat=Price("19.50", "CHF"))
        row = BitcoinInvoice.objects.create(
            payment=payment, ref=invoice.ref, data=invoice.to_json()
        )
    fake_sdk.statuses = [PaymentStatus.paid_lightning(PREIMAGE)]
    assert checks.check(row.pk)
    row = reload(row)
    assert row.payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
    order.refresh_from_db()
    assert order.status == Order.STATUS_PAID
    assert entries(order, "pretix_sattleme.payment.changed") == []


@pytest.mark.django_db
def test_the_customer_is_told_and_offered_no_other_way_to_pay(
    client, fake_sdk, provider, invoice_row, order, payment, event
):
    enable_bank_transfer(event)
    order = change_total(order, "30.00")
    fake_sdk.statuses = [PaymentStatus.paid_lightning(PREIMAGE)]
    checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert row.phase(row.payment) == "review"
    assert row.may_be_paid()

    context = display.context(row, row.payment)
    assert context["qr"] is None and context["poll_interval"] == 30
    html = client.get(f"/org/conf/order/{order.code}/{order.secret}/").content.decode()
    assert "the organizer settles the difference with you" in html
    with scopes_disabled():
        pay = client.get(links.pay_url(row.payment))
    html = pay.content.decode()
    assert pay.status_code == 200 and "<svg" not in html
    assert "the organizer settles the difference with you" in html
    page = pay_change(client, order)
    assert not (
        page.status_code == 200 and 'value="banktransfer"' in page.content.decode()
    )
    with scopes_disabled():
        assert not provider._payment_abort_pending_allowed(row.payment)
        second = order.payments.create(
            provider="sattleme", state="created", amount=Decimal("30.00")
        )
        with pytest.raises(PaymentException, match="awaits the organizer"):
            invoices.create(provider, OrderPayment.objects.get(pk=second.pk))


@pytest.mark.django_db
def test_the_organizer_confirms_by_hand_or_cancels(
    client, fake_sdk, provider, invoice_row, order, payment, admin
):
    order = change_total(order, "30.00")
    fake_sdk.statuses = [PaymentStatus.paid_lightning(PREIMAGE)]
    checks.check(invoice_row.pk)
    client.force_login(admin)
    html = client.get(f"/control/event/org/conf/orders/{order.code}/").content.decode()
    assert "Not confirmed: the order owed another amount when it was paid" in html

    with scopes_disabled():
        payment = OrderPayment.objects.get(pk=payment.pk)
        payment.confirm()
        order.refresh_from_db()
    # 24.50 of 30.00: the customer pays the rest, by any method.
    assert order.status == Order.STATUS_PENDING
    assert order.pending_sum == Decimal("5.50")
    row = reload(invoice_row)
    assert row.phase(row.payment) == "paid" and not row.may_be_paid()


@pytest.mark.django_db
def test_the_organizer_may_cancel_a_payment_left_to_them(
    fake_sdk, provider, invoice_row, order, payment
):
    order = change_total(order, "30.00")
    fake_sdk.statuses = [PaymentStatus.paid_lightning(PREIMAGE)]
    checks.check(invoice_row.pk)
    with scopes_disabled():
        payment = OrderPayment.objects.get(pk=payment.pk)
        provider.cancel_payment(payment)
    row = reload(invoice_row)
    assert row.payment.state == OrderPayment.PAYMENT_STATE_CANCELED
    assert row.phase(row.payment) == "canceled" and not row.may_be_paid()


@pytest.mark.parametrize(
    "status, noted",
    [
        (PaymentStatus.paid_lightning(PREIMAGE), None),
        (PaymentStatus.paid_onchain(49_000, (TXID,)), None),
        (PaymentStatus.paid_onchain(52_000, (TXID,)), "52000"),
        (PaymentStatus.paid_mixed(48_500, (TXID,), (PREIMAGE,)), "48500"),
        # The merchant's acceptance (SPEC.md §31): what an order underpaid on-chain received,
        # accepted by hand in the sattle.me app, far below any tolerance.
        (PaymentStatus.paid_onchain(20_000, (TXID,)), "20000"),
        (PaymentStatus.paid_mixed(30_000, (TXID,), (PREIMAGE,)), "30000"),
    ],
    ids=["lightning", "exact", "overpaid", "tolerance", "accepted", "accepted-mixed"],
)
@pytest.mark.django_db
def test_a_payment_of_another_amount_in_sats_is_noted(
    fake_sdk, invoice_row, order, status, noted
):
    fake_sdk.statuses = [status]
    assert checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert row.payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
    notes = entries(order, "pretix_sattleme.payment.amount")
    if noted is None:
        assert notes == []
    else:
        [note] = notes
        assert note.parsed_data == {"local_id": 1, "received": noted, "sats": "49000"}
        assert "of 49,000 sats" in str(note.display())
