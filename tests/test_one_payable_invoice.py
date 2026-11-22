"""One payable invoice per order: another payment method, or a new Bitcoin invoice, only once the
last invoice ended unpaid, which is known only from its final status: `expired` with `received`
0, from a check after its end (`expiresAt` + 120 s). Until then its time being over is not
enough: a Lightning payment of its last seconds, or an on-chain one only BlockCypher dates in
time, is found by that check, however late it runs (cron without Celery workers) or long it
fails (the merchant's Lightning address not answering).

From the audit before the first release (its proofs: test_switch_before_final.py), through
pretix's own pages. pretix 2026.7 never lets the customer leave a pending payment of this method;
pretix 2026.8 and later ask the method (_payment_abort_pending_allowed)."""

import time
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils.timezone import now
from django_scopes import scopes_disabled
from pretix.base.models import Order, OrderPayment
from pretix.base.payment import PaymentException
from sattleme_checkout import NetworkError, PaymentStatus

from pretix_sattleme import checks, invoices
from pretix_sattleme.models import BitcoinInvoice

from .conftest import make_invoice

PREIMAGE = "cd" * 32


def ended_unchecked(invoice_row, last_status=PaymentStatus.open(1, 0, None)):
    """The invoice ended 30 s ago (its grace period included); its last successful check was
    before its end, and the checks since failed (the Lightning address did not answer).
    """
    invoice = make_invoice(created=int(time.time()) - 900 - 120 - 30)
    with scopes_disabled():
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(
            data=invoice.to_json(),
            status=last_status.to_json(),
            checked=now() - timedelta(seconds=200),
            error="network",
            errors=2,
        )


def reload(row):
    with scopes_disabled():
        return BitcoinInvoice.objects.select_related("payment__order").get(pk=row.pk)


def enable_bank_transfer(event):
    with scopes_disabled():
        event.plugins = "pretix_sattleme,pretix.plugins.banktransfer"
        event.save()
        # A plugin of both levels on pretix 2026.8: enabled for the organizer too.
        event.organizer.plugins = "pretix.plugins.banktransfer"
        event.organizer.save()
        event.settings.set("payment_banktransfer__enabled", True)


def change_url(order):
    return f"/org/conf/order/{order.code}/{order.secret}/pay/change"


def offered(page):
    return page.status_code == 200 and 'value="banktransfer"' in page.content.decode()


@pytest.mark.django_db
def test_no_other_method_while_the_final_status_is_unknown(
    provider, invoice_row, payment
):
    ended_unchecked(invoice_row)
    with scopes_disabled():
        payment = OrderPayment.objects.get(pk=payment.pk)
        assert payment.state == OrderPayment.PAYMENT_STATE_PENDING
        assert not provider._payment_abort_pending_allowed(payment)


@pytest.mark.django_db
def test_the_order_is_never_paid_twice_by_switching(
    client, fake_sdk, invoice_row, payment, order, event
):
    """Through pretix's own pages: the customer cannot switch to bank transfer before the
    invoice's final status is known; the Lightning invoice, paid in its last minute, is then
    found paid and pays the order, alone."""
    enable_bank_transfer(event)
    ended_unchecked(invoice_row)

    assert not offered(client.get(change_url(order)))
    client.post(change_url(order), {"payment": "banktransfer"})
    with scopes_disabled():
        payment.refresh_from_db()
        assert payment.state == OrderPayment.PAYMENT_STATE_PENDING
        assert not order.payments.exclude(pk=payment.pk).exists()

    fake_sdk.statuses = [PaymentStatus.paid_lightning(PREIMAGE)]
    assert checks.check(invoice_row.pk)
    with scopes_disabled():
        payment.refresh_from_db()
        order.refresh_from_db()
        assert not order.payments.exclude(pk=payment.pk).exists()
    assert payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
    assert order.status == Order.STATUS_PAID


@pytest.mark.django_db
def test_once_a_check_after_the_end_found_it_unpaid_the_customer_may_switch(
    client, fake_sdk, provider, invoice_row, payment, order, event
):
    enable_bank_transfer(event)
    ended_unchecked(invoice_row)
    fake_sdk.statuses = [PaymentStatus.expired(0)]
    assert checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert row.final and not row.may_be_paid()
    assert row.payment.state == OrderPayment.PAYMENT_STATE_FAILED
    assert offered(client.get(change_url(order)))
    with scopes_disabled():
        second = order.payments.create(
            provider="sattleme", state="created", amount=order.total
        )
        assert invoices.create(provider, second).made


@pytest.mark.django_db
def test_a_check_failing_after_the_end_keeps_it_payable(fake_sdk, invoice_row):
    """When the checks after the end fail, the final status stays unknown: the invoice may have
    been paid, it is still the order's payable invoice."""
    ended_unchecked(invoice_row)
    fake_sdk.statuses = [NetworkError("the Lightning address did not answer")]
    checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert not row.final and row.state == "open"
    assert row.may_be_paid()


@pytest.mark.django_db
def test_no_new_invoice_while_the_final_status_is_unknown(
    fake_sdk, provider, invoice_row, payment, order
):
    """A second Bitcoin payment of the order gets no invoice, of a new reference, while the
    first one's final status is unknown: an on-chain payment of the first one's last seconds,
    found later through BlockCypher, would pay the order on top of the second."""
    ended_unchecked(invoice_row)
    with scopes_disabled():
        OrderPayment.objects.filter(pk=payment.pk).update(state="canceled")
        second = order.payments.create(
            provider="sattleme", state="created", amount=Decimal("24.50")
        )
        with pytest.raises(PaymentException, match="known to be unpaid"):
            invoices.create(provider, OrderPayment.objects.get(pk=second.pk))
    assert fake_sdk.created == []


@pytest.mark.django_db
def test_an_invoice_ended_underpaid_keeps_the_order_for_the_organizer(
    client, fake_sdk, provider, invoice_row, payment, order, event
):
    """`expired` with `received` above 0 did not end unpaid: what came reached the merchant's
    wallet, and paying again would pay the order twice. The customer is asked to contact the
    organizer, who settles it: here, cancels the payment for the customer to pay again.
    """
    enable_bank_transfer(event)
    ended_unchecked(invoice_row)
    fake_sdk.statuses = [PaymentStatus.expired(30_000)]
    assert checks.check(invoice_row.pk)
    row = reload(invoice_row)
    assert row.final and row.may_be_paid()
    assert row.payment.state == OrderPayment.PAYMENT_STATE_PENDING
    html = client.get(f"/org/conf/order/{order.code}/{order.secret}/").content.decode()
    assert "Please contact the organizer to settle the difference" in html
    assert not offered(client.get(change_url(order)))
    with scopes_disabled():
        assert not provider._payment_abort_pending_allowed(row.payment)
        second = order.payments.create(
            provider="sattleme", state="created", amount=Decimal("24.50")
        )
        with pytest.raises(PaymentException, match="awaits the organizer"):
            invoices.create(provider, OrderPayment.objects.get(pk=second.pk))
        OrderPayment.objects.filter(pk=second.pk).delete()

        provider.cancel_payment(OrderPayment.objects.get(pk=payment.pk))
    assert not reload(invoice_row).may_be_paid()
    assert offered(client.get(change_url(order)))
