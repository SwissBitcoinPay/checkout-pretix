import json
from decimal import Decimal
from typing import Any

from django.db.models.signals import pre_save
from django.utils.formats import number_format
from django.utils.translation import gettext_lazy as _
from pretix.base.logentrytypes import OrderLogEntryType, log_entry_types
from pretix.base.models import LogEntry
from pretix.base.signals import periodic_task, register_payment_providers
from pretix.base.templatetags.money import money_filter

# pretix's log entry of a payment method's settings saved, which holds their new values; and the
# mark pretix writes in place of the values it hides from its log entries.
SETTINGS_SAVED = "pretix.event.payment.provider"
REDACTED = "********"


# The receivers are connected below, as Django's @receiver connects them.


def register_payment_provider(sender: Any, **kwargs: Any) -> type:
    from .payment import Sattleme

    return Sattleme


def keep_the_key_out_of_the_log(sender: Any, instance: Any, **kwargs: Any) -> None:
    """pretix logs the new values of a payment method's settings, a new integration key
    included (it hides only keys of its data named like a password, a secret or an API key):
    it is hidden there, before the entry is written."""
    from .sdk import KEY_SETTING

    if instance.action_type != SETTINGS_SAVED or KEY_SETTING not in (
        instance.data or ""
    ):
        return
    data = json.loads(instance.data)
    values = data.get("new_values")
    if isinstance(values, dict) and values.get(KEY_SETTING):
        values[KEY_SETTING] = REDACTED
        instance.data = json.dumps(data, sort_keys=True)


def sweep_invoices(sender: Any, **kwargs: Any) -> None:
    from . import checks

    checks.sweep()


register_payment_providers.connect(
    register_payment_provider, dispatch_uid="payment_sattleme"
)
pre_save.connect(
    keep_the_key_out_of_the_log,
    sender=LogEntry,
    dispatch_uid="payment_sattleme_log_no_key",
)
periodic_task.connect(sweep_invoices, dispatch_uid="payment_sattleme_sweep")


@log_entry_types.new_from_dict(
    {
        "pretix_sattleme.invoice.created": _(
            "A Bitcoin invoice of {sats} sats was created for payment {local_id}."
        ),
        "pretix_sattleme.invoice.recovered": _(
            "The Bitcoin invoice of {sats} sats made earlier for payment {local_id} was "
            "found again in the merchant's registry, and shown again."
        ),
        "pretix_sattleme.invoice.failed": _(
            "No Bitcoin invoice could be created ({error})."
        ),
        "pretix_sattleme.payment.unconfirmed": _(
            "A Bitcoin payment of {received} sats came, and is being confirmed on-chain."
        ),
        "pretix_sattleme.payment.underpaid": _(
            "The Bitcoin invoice ended underpaid: {received} of {sats} sats came, and "
            "reached your wallet. Please settle the difference with the customer."
        ),
        "pretix_sattleme.payment.changed": _(
            "The Bitcoin invoice of payment {local_id} was paid, {amount} ({received} sats), "
            "while the order owes {owed} now: it changed after the invoice was made. The "
            "payment was not confirmed: please settle the difference with the customer, then "
            "confirm it by hand."
        ),
        "pretix_sattleme.payment.amount": _(
            "The Bitcoin invoice of payment {local_id} was paid with {received} sats, for an "
            "amount of {sats} sats: less is what the merchant accepted in the sattle.me app "
            "(its tolerance, or by hand), more an overpayment."
        ),
        "pretix_sattleme.check.abandoned": _(
            "The Bitcoin invoice of payment {local_id} had no final status after {days} "
            "days: its checks stopped. Please check this payment in the sattle.me app."
        ),
    }
)
class SattlemeLogEntryType(OrderLogEntryType):
    # Amounts in sats as the payment's details show them (47,369). Never the invoice's
    # reference, which the payment's details show: too long for the order's history, it would
    # overflow its column.
    AMOUNTS = ("sats", "received")
    # Amounts in the order's currency, as pretix shows them (its money filter).
    MONEY = ("amount", "owed")

    def display(self, logentry: LogEntry, data: dict[str, Any]) -> str:
        from .display import error_text

        data = dict(data)
        if data.get("error"):
            data["error"] = error_text(data["error"])
        for key in self.AMOUNTS:
            if str(data.get(key, "")).isdigit():
                data[key] = number_format(int(data[key]), force_grouping=True)
        for key in self.MONEY:
            if key in data and data.get("currency"):
                data[key] = money_filter(Decimal(data[key]), data["currency"])
        text: str = super().display(logentry, data)
        return text
