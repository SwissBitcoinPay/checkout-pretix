import json

import pytest
from django.utils.timezone import now
from django_scopes import scopes_disabled
from pretix.base.models import OrderPayment
from pretix.base.shredder import PaymentInfoShredder
from sattleme_checkout import PaymentStatus

from pretix_sattleme import checks, shredding
from pretix_sattleme.models import BitcoinInvoice

from .conftest import ADDRESS, make_invoice

TXID = "ef" * 32
# pretix's log entry of the order marked paid, which copies the payment's information.
PAID_ACTION = "pretix.event.order.paid"
PREIMAGE = "cd" * 32
SHREDDED = shredding.SHREDDED


def onchain_row(payment):
    """A payment with an invoice payable on-chain too, as execute_payment leaves them."""
    invoice = make_invoice(onchain=True)
    with scopes_disabled():
        row = BitcoinInvoice.objects.create(
            payment=payment, ref=invoice.ref, data=invoice.to_json(), next_check=now()
        )
        payment.state = OrderPayment.PAYMENT_STATE_PENDING
        payment.info_data = row.info()
        payment.save(update_fields=["state", "info"])
    return row


def shred(event):
    """pretix's data shredder « Payment information », as the organizer runs it."""
    with scopes_disabled():
        PaymentInfoShredder(event).shred_data()


def reload(row):
    with scopes_disabled():
        return BitcoinInvoice.objects.select_related("payment").get(pk=row.pk)


def everything(row):
    """All this plugin and pretix keep of the payment of `row`, as one text."""
    with scopes_disabled():
        row = reload(row)
        entries = [e.data for e in row.payment.order.all_logentries()]
        return json.dumps([row.data, row.status, row.payment.info_data, entries])


@pytest.mark.django_db
def test_a_paid_onchain_payment_loses_its_address_and_transactions(
    fake_sdk, provider, event, payment, rf
):
    row = onchain_row(payment)
    fake_sdk.statuses = [PaymentStatus.paid_onchain(49_000, (TXID,))]
    checks.check(row.pk)
    assert ADDRESS in everything(row) and TXID in everything(row)
    original = reload(row).data

    shred(event)

    row = reload(row)
    assert ADDRESS not in everything(row) and TXID not in everything(row)
    assert row.shredded
    assert row.data["onchainAddress"] == SHREDDED
    assert row.data["paymentUri"] == SHREDDED
    assert row.status == {
        "state": "paid",
        "method": "onchain",
        "received": "49000",
        "txids": [],
    }
    # What is not personal data on its own stays: the sale's reference and amounts.
    for key in ("ref", "sats", "fiat", "bolt11", "paymentHash", "expiresAt"):
        assert row.data[key] == original[key]
    info = row.payment.info_data
    assert info["ref"] == row.ref and info["sats"] == "49000"
    assert info["onchain_address"] == SHREDDED and info["_shredded"] is True
    assert row.payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED

    # The backend and the API still show the payment, without them.
    with scopes_disabled():
        control = provider.payment_control_render(rf.get("/"), row.payment)
        details = provider.api_payment_details(row.payment)
    assert row.ref in control and ADDRESS not in control and TXID not in control
    assert details["onchain_address"] == SHREDDED and details["ref"] == row.ref
    # pretix's own log entry of the order paid kept the information as JSON text: still JSON.
    with scopes_disabled():
        [paid] = row.payment.order.all_logentries().filter(action_type=PAID_ACTION)
    info = json.loads(json.loads(paid.data)["info"])
    assert paid.shredded and info["ref"] == row.ref and info["_shredded"] is True


@pytest.mark.django_db
def test_a_failed_payment_loses_them_in_its_log_entry_too(
    fake_sdk, event, payment, order
):
    row = onchain_row(payment)
    fake_sdk.statuses = [PaymentStatus.expired(0)]
    checks.check(row.pk)
    with scopes_disabled():
        [failed] = order.all_logentries().filter(action_type=shredding.FAILED_ACTION)
    assert failed.parsed_data["info"]["onchain_address"] == ADDRESS

    shred(event)

    with scopes_disabled():
        failed.refresh_from_db()
    data = json.loads(failed.data)
    assert failed.shredded
    assert data["info"]["onchain_address"] == SHREDDED
    assert data["local_id"] == payment.local_id
    assert ADDRESS not in everything(row)


@pytest.mark.django_db
def test_an_invoice_being_checked_keeps_what_its_checks_need_until_final(
    fake_sdk, event, payment
):
    row = onchain_row(payment)
    fake_sdk.statuses = [PaymentStatus.unconfirmed(49_000, 1, (TXID,))]
    checks.check(row.pk)

    shred(event)

    # Its checks still read the address: the payment's information has none already.
    row = reload(row)
    assert row.shredded and not row.final
    assert row.data["onchainAddress"] == ADDRESS
    assert row.payment.info_data["onchain_address"] == SHREDDED

    fake_sdk.statuses = [PaymentStatus.paid_onchain(49_000, (TXID,))]
    checks.check(row.pk)
    row = reload(row)
    assert row.final and row.payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
    # The check found the address again, and wrote the transaction: final, both go.
    assert fake_sdk.checked[-1][0]["onchainAddress"] == ADDRESS
    assert ADDRESS not in everything(row) and TXID not in everything(row)


@pytest.mark.django_db
def test_an_invoice_given_up_on_loses_them(fake_sdk, event, payment):
    row = onchain_row(payment)
    fake_sdk.statuses = [PaymentStatus.unconfirmed(49_000, 1, (TXID,))]
    checks.check(row.pk)
    shred(event)

    with scopes_disabled():
        checks._give_up(reload(row))
    row = reload(row)
    assert row.final and row.error == "abandoned"
    assert ADDRESS not in everything(row) and TXID not in everything(row)


@pytest.mark.django_db
def test_a_lightning_payment_keeps_its_invoice(fake_sdk, event, invoice_row):
    # A Lightning invoice names the merchant's node, never the customer's: nothing to remove.
    fake_sdk.statuses = [PaymentStatus.paid_lightning(PREIMAGE)]
    checks.check(invoice_row.pk)
    before = reload(invoice_row)

    shred(event)
    shred(event)  # Twice: nothing more goes.

    row = reload(invoice_row)
    assert row.shredded
    assert row.data == before.data and row.status == before.status
    assert row.payment.info_data == {**before.payment.info_data, "_shredded": True}


@pytest.mark.django_db
def test_other_providers_payments_are_pretixs_to_scrub(event, order):
    with scopes_disabled():
        other = order.payments.create(
            provider="manual", state="confirmed", amount=1, info='{"note": "cash"}'
        )
    shred(event)
    with scopes_disabled():
        other.refresh_from_db()
    assert other.info_data == {}
