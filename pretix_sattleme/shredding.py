"""Removing what leads to a customer's coins, when pretix removes an event's personal data.

pretix's data shredder « Payment information » has each payment provider scrub its payments
(`shred_payment_info`). A Bitcoin payment holds no name nor email, but an on-chain one is public:
the deposit address its customer paid, and the transactions that paid it, lead anyone to the
customer's coins and wallet on the blockchain. Those go, from the payment's information, from
pretix's log entries of the payment, and from its invoice. What stays is not personal data on its
own, as pretix's own providers keep it: the invoice's reference, amounts, rate, dates and state,
its Lightning invoice and payment hash, and the proof of a Lightning payment (a Lightning
invoice names the merchant's node, never the customer's).

An invoice whose final status is not known yet (an on-chain payment being confirmed) keeps what
its checks need until it is final, then loses it (`finished`).
"""

import json
from typing import Any

from django.db import transaction
from django_scopes import scopes_disabled
from pretix.base.models import OrderPayment

from .models import BitcoinInvoice

# What replaces a value removed, as pretix's own payment providers write it.
SHREDDED = "█"

# pretix's log entries that copy a payment's information: of a failed payment (its information
# as given, with the payment's number), and of the order marked paid by a payment (its
# information as stored, a JSON text, without the payment's number).
FAILED_ACTION = "pretix.event.order.payment.failed"
PAID_ACTION = "pretix.event.order.paid"


def shred_status(status: dict[str, Any]) -> dict[str, Any]:
    """A status without the transactions that paid the invoice, nor the rest to pay (whose QR
    code holds the address)."""
    status = dict(status)
    if status.get("txids"):
        status["txids"] = []
    if status.get("rest"):
        status["rest"] = None
    return status


def shred_invoice(data: dict[str, Any]) -> dict[str, Any]:
    """An invoice without its deposit address, which its payment URI (BIP-21) holds too."""
    data = dict(data)
    if data.get("onchainAddress"):
        data["onchainAddress"] = SHREDDED
        data["paymentUri"] = SHREDDED
    return data


def shred_info(info: dict[str, Any]) -> dict[str, Any]:
    """A payment's information (BitcoinInvoice.info) without the address and transactions."""
    info = dict(info)
    if info.get("onchain_address"):
        info["onchain_address"] = SHREDDED
    if isinstance(info.get("status"), dict):
        info["status"] = shred_status(info["status"])
    info["_shredded"] = True
    return info


def shred_payment(payment: OrderPayment) -> None:
    """Scrub a payment of this plugin: its information, its log entries, and its invoice now if
    final, else once it is (`finished`)."""
    with scopes_disabled():
        row = BitcoinInvoice.objects.filter(payment=payment).first()
        if row is not None:
            BitcoinInvoice.objects.filter(pk=row.pk).update(shredded=True)
            _shred_invoice_if_final(row.pk)
        _shred_payment_info(payment)


def finished(row: BitcoinInvoice) -> None:
    """An invoice became final: if its event's personal data was removed meanwhile, it loses
    what its checks needed, and its payment what a check wrote since."""
    with scopes_disabled():
        if not BitcoinInvoice.objects.filter(pk=row.pk, shredded=True).exists():
            return
        _shred_invoice_if_final(row.pk)
        payment = OrderPayment.objects.select_related("order").get(pk=row.payment_id)
        _shred_payment_info(payment)


def _shred_invoice_if_final(invoice_id: int) -> None:
    with transaction.atomic(), scopes_disabled():
        row = (
            BitcoinInvoice.objects.select_for_update()
            .filter(pk=invoice_id, final=True)
            .first()
        )
        if row is None:
            return
        row.data = shred_invoice(row.data)
        if row.status:
            row.status = shred_status(row.status)
        row.save(update_fields=["data", "status"])


def _shred_payment_info(payment: OrderPayment) -> None:
    with scopes_disabled():
        payment.refresh_from_db(fields=["info"])
        if payment.info_data:
            payment.info_data = shred_info(payment.info_data)
            payment.save(update_fields=["info"])
        entries = payment.order.all_logentries().filter(
            action_type__in=(FAILED_ACTION, PAID_ACTION)
        )
        for entry in entries:
            data = entry.parsed_data
            if data.get("provider") != payment.provider:
                continue
            if entry.action_type == FAILED_ACTION and (
                data.get("local_id") != payment.local_id
            ):
                continue
            info = _info_of(data.get("info"))
            if info is None:
                continue
            shredded = shred_info(info)
            data["info"] = (
                json.dumps(shredded, sort_keys=True)
                if isinstance(data["info"], str)
                else shredded
            )
            entry.data = json.dumps(data)
            entry.shredded = True
            entry.save(update_fields=["data", "shredded"])


def _info_of(value: object) -> dict[str, Any] | None:
    """A payment's information in a log entry, as given (a dict) or as stored (JSON text)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None
