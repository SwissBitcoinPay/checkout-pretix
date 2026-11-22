import logging
from collections import OrderedDict
from decimal import Decimal
from typing import Any

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.http import HttpRequest
from django.template.loader import get_template
from django.utils.translation import gettext, gettext_lazy as _
from django_scopes import scopes_disabled
from i18nfield.forms import I18nFormField, I18nTextInput
from i18nfield.strings import LazyI18nString
from pretix.base.forms import (
    SECRET_REDACTED,
    I18nMarkdownTextarea,
    SecretKeySettingsField,
    SecretKeySettingsWidget,
)
from pretix.base.models import Order, OrderFee, OrderPayment
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.base.templatetags.rich_text import rich_text
from pretix.helpers import OF_SELF
from sattleme_checkout import ChannelNotFoundError, InvalidKeyError, RevokedError

from . import display, invoices, links, prices, sdk, shredding
from .models import BitcoinInvoice

logger = logging.getLogger(__name__)

# Why a payment whose invoice may still be paid is not canceled: pretix logs it on the order, and
# shows it to the organizer who asked.
NOT_CANCELED = _(
    "The Bitcoin invoice of this payment may still be paid: the payment can be canceled once "
    "the invoice's final status is known."
)


def verify_integration_key(secret: str) -> None:
    """Check a key an organizer enters: an integration key, of an integration the merchant
    still has (the SDK reads its channel). No frame of this check keeps the key's text.
    """
    try:
        client = sdk.new_client(secret)
    except InvalidKeyError:
        raise ValidationError(
            _("This is not the secret key of a sattle.me integration (sattleme_ck_…).")
        ) from None
    except Exception:
        # Never expected. Not raised as it is: the frames of its traceback (Django's, which
        # called this check) hold the key, and an error tracker would record them.
        logger.error("Could not read a sattle.me integration key")
        raise ValidationError(
            _("This is not the secret key of a sattle.me integration (sattleme_ck_…).")
        ) from None
    finally:
        del secret
    try:
        client.merchant(timeout=sdk.VERIFY_TIMEOUT)
    except RevokedError:
        raise ValidationError(
            _(
                "This integration was removed in the sattle.me app. Create a new one and "
                "enter its key."
            )
        ) from None
    except ChannelNotFoundError as e:
        if e.newer_format:
            raise ValidationError(
                _("This integration needs a newer version of the sattle.me plugin.")
            ) from None
        raise ValidationError(
            _(
                "This integration could not be found. Please check the key, or try again "
                "in a moment."
            )
        ) from None
    except Exception as e:
        logger.warning(
            "Could not verify a sattle.me integration key: %s", sdk.describe(e)
        )
        raise ValidationError(
            _(
                "This key could not be verified: the integration's relays or its Lightning "
                "address did not answer. Please try again in a moment."
            )
        ) from None


class IntegrationKeyWidget(SecretKeySettingsWidget):
    """Never the text of a key: one saved shows as SECRET_REDACTED, one just entered (refused,
    the form shown again) as nothing. pretix's own widget would send that one back."""

    def get_context(self, name: str, value: Any, attrs: Any) -> dict[str, Any]:
        context: dict[str, Any] = forms.TextInput.get_context(
            self, name, SECRET_REDACTED if value else "", attrs
        )
        return context


class IntegrationKeyField(SecretKeySettingsField):
    """The integration key: write-only (never shown, never sent back to the browser), verified
    when it changes."""

    widget = IntegrationKeyWidget
    default_validators = [verify_integration_key]

    def bound_data(self, data: Any, initial: Any) -> Any:
        # What a form shown again holds: the key saved (shown redacted), never the one entered.
        return initial


def record_of(payment: OrderPayment) -> BitcoinInvoice | None:
    """The record of the payment's invoice (BitcoinInvoice), made or not, or None."""
    try:
        row: BitcoinInvoice = payment.sattleme_invoice
    except BitcoinInvoice.DoesNotExist:
        return None
    return row


def invoice_of(payment: OrderPayment) -> BitcoinInvoice | None:
    """The payment's invoice once the SDK made it, else None."""
    row = record_of(payment)
    return row if row is not None and row.made else None


class Sattleme(BasePaymentProvider):
    identifier = "sattleme"
    verbose_name = _("Bitcoin (sattle.me)")
    # The customer may switch to another method only once the invoice can no longer be paid:
    # see _payment_abort_pending_allowed.
    abort_pending_allowed = False
    execute_payment_needs_user = True

    @property
    def public_name(self) -> str:
        # What the customer sees: the organizer's name of the method (in the customer's
        # language), else "Bitcoin".
        return str(
            self.settings.get("public_name", as_type=LazyI18nString) or _("Bitcoin")
        )

    @property
    def test_mode_message(self) -> str:
        return str(
            _(
                "sattle.me has no test mode: in test mode too, payments are real bitcoin "
                "payments, to the organizer's wallet."
            )
        )

    @property
    def settings_form_fields(self) -> dict[str, Any]:
        base = super().settings_form_fields
        minutes = forms.IntegerField(
            label=_("Time to pay"),
            help_text=_(
                "How many minutes the customer has to pay a Bitcoin invoice, from 1 to 60."
            ),
            min_value=1,
            max_value=60,
            initial=invoices.DEFAULT_MINUTES,
            required=True,
        )
        minutes._as_type = int
        fields = OrderedDict([("_enabled", base.pop("_enabled"))])
        # As pretix's own payment methods name them.
        fields["public_name"] = I18nFormField(
            label=_("Payment method name"),
            help_text=_(
                "What the customer sees at checkout, on the order's page and in emails. "
                "Empty: “Bitcoin”."
            ),
            widget=I18nTextInput,
            required=False,
        )
        fields["checkout_description"] = I18nFormField(
            label=_("Payment process description during checkout"),
            help_text=_(
                "Shown to the customer who chooses this payment method. Empty: how a "
                "Bitcoin payment goes, in the customer's language."
            ),
            widget=I18nMarkdownTextarea,
            required=False,
        )
        fields["key"] = IntegrationKeyField(
            label=_("Integration key"),
            help_text=_(
                "In the sattle.me app, create an integration for this shop and enter its "
                "secret key (sattleme_ck_…). Whoever holds it can create invoices that pay "
                "you, never spend your funds: keep it secret all the same. "
                "Change it only while no Bitcoin payment is pending: those are checked "
                "with the key set here."
            ),
            required=True,
        )
        fields["invoice_minutes"] = minutes
        fields.update(base)
        return fields

    def settings_content_render(self, request: HttpRequest) -> str:
        currency: str = self.event.currency
        html: str = get_template("pretix_sattleme/settings.html").render(
            {
                "event": self.event,
                "currency": currency,
                # When the method cannot be offered, or only for some amounts (prices.py).
                "currency_usable": prices.usable(currency),
                "whole_units_only": prices.whole_units_only(currency),
            },
            request=request,
        )
        return html

    def _configured(self) -> bool:
        return bool(self.settings.get("key"))

    def _takes(self, amount: Decimal | None) -> bool:
        """Whether the SDK takes the price of a payment of `amount` (prices.py): never in a
        currency out of its table, whatever the amount."""
        currency: str = self.event.currency
        if not prices.usable(currency):
            return False
        return amount is None or prices.price(amount, currency) is not None

    def is_allowed(self, request: HttpRequest, total: Any = None) -> bool:
        # `total` is the order's, without this method's fee: the payment is of both, as
        # pretix's checkout shows it with this method (the payment step's provider_forms).
        amount = None if total is None else total + self.calculate_fee(total)
        return (
            self._configured()
            and self._takes(amount)
            and super().is_allowed(request, total)
        )

    def order_change_allowed(
        self, order: Order, request: HttpRequest | None = None
    ) -> bool:
        return (
            self._configured()
            and self._takes(self._changed_payment_amount(order))
            and super().order_change_allowed(order, request=request)
        )

    def _changed_payment_amount(self, order: Order) -> Decimal:
        """What the order's payment comes to once its customer changes to this method, as
        pretix's page of payment methods shows it (OrderPayChangeMethod.provider_forms): what
        the order owes, its payment fees that no confirmed or refunded payment holds
        replaced by this method's.
        """
        held = OrderPayment.objects.filter(
            fee=OuterRef("pk"),
            state__in=(
                OrderPayment.PAYMENT_STATE_CONFIRMED,
                OrderPayment.PAYMENT_STATE_REFUNDED,
            ),
        )
        open_fees = (
            order.fees.annotate(has_payment=Exists(held))
            .filter(fee_type=OrderFee.FEE_TYPE_PAYMENT, has_payment=False)
            .values_list("value", flat=True)
        )
        owed: Decimal = order.pending_sum - sum(open_fees, Decimal("0.00"))
        fee: Decimal = self.calculate_fee(owed)
        return owed + fee

    def payment_form_render(
        self, request: HttpRequest, total: Any = None, order: Order | None = None
    ) -> str:
        description = self.settings.get("checkout_description", as_type=LazyI18nString)
        html: str = get_template("pretix_sattleme/checkout_payment_form.html").render(
            {
                "request": request,
                "event": self.event,
                # The organizer's text, as pretix's own methods render it (Markdown, cleaned).
                "description": rich_text(str(description)) if description else None,
            },
            request=request,
        )
        return html

    def checkout_confirm_render(
        self, request: HttpRequest, order: Order | None = None, info_data: Any = None
    ) -> str:
        template = get_template("pretix_sattleme/checkout_payment_confirm.html")
        html: str = template.render(
            {"request": request, "event": self.event}, request=request
        )
        return html

    def payment_is_valid_session(self, request: HttpRequest) -> bool:
        return True

    def execute_payment(self, request: HttpRequest, payment: OrderPayment) -> str:
        invoices.create(self, payment)
        return links.pay_url(payment)

    def payment_pending_render(
        self, request: HttpRequest, payment: OrderPayment
    ) -> str:
        row = invoice_of(payment)
        if row is None:
            return ""
        context = display.context(row, payment, with_qr=False)
        context["pay_url"] = links.pay_url(payment)
        html: str = get_template("pretix_sattleme/pending.html").render(
            context, request=request
        )
        return html

    def order_pending_mail_render(self, order: Order, payment: OrderPayment) -> str:
        text: str = gettext(
            "To pay with Bitcoin, open your order with the link below: its payment page "
            "shows a QR code to scan with your Bitcoin wallet."
        )
        return text

    def payment_control_render(
        self, request: HttpRequest, payment: OrderPayment
    ) -> str:
        row = record_of(payment)
        if row is None:
            return ""
        html: str = get_template("pretix_sattleme/control.html").render(
            display.control_context(row, payment), request=request
        )
        return html

    def api_payment_details(self, payment: OrderPayment) -> dict[str, Any]:
        row = invoice_of(payment)
        if row is None:
            return {}
        return {
            "ref": row.ref,
            "sats": row.sats,
            "payment_hash": row.payment_hash,
            "onchain_address": row.onchain_address,
            "expires_at": row.expires_at,
            "status": row.status,
            "final": row.final,
        }

    def matching_id(self, payment: OrderPayment) -> str | None:
        row = invoice_of(payment)
        return str(row.ref) if row else None

    def _payment_abort_pending_allowed(self, payment: OrderPayment) -> bool:
        # pretix 2026.8 and later ask this before letting the customer choose another method
        # while the payment is pending (earlier versions read abort_pending_allowed alone). Not
        # while the invoice may be paid, its final status unknown: a Lightning invoice cannot
        # be revoked, the order could be paid twice. Nor while what came is being confirmed.
        row = invoice_of(payment)
        return row is None or not row.may_be_paid()

    def shred_payment_info(self, obj: Any) -> None:
        # pretix removes the event's personal data: the address and transactions that lead to
        # the customer's coins go, the invoice's reference and amounts stay (shredding.py).
        if isinstance(obj, OrderPayment):
            shredding.shred_payment(obj)
        else:
            super().shred_payment_info(obj)

    def cancel_payment(self, payment: OrderPayment) -> None:
        # pretix cancels the open payment when the order is canceled, changed or paid
        # otherwise, or when the organizer cancels it. Not while its invoice may be paid (a
        # Lightning invoice cannot be revoked): pretix then keeps the payment pending, and logs
        # why. Its checks go on; a payment found paid once the order owes another amount is
        # left to the organizer (checks._paid).
        with transaction.atomic(), scopes_disabled():
            # Locked as invoices.create locks it: an invoice being made is seen once stored.
            OrderPayment.objects.select_for_update(of=OF_SELF).get(pk=payment.pk)
            row = BitcoinInvoice.objects.filter(payment_id=payment.pk).first()
            if row is not None and row.made and not row.final:
                raise PaymentException(NOT_CANCELED)
            payment.state = OrderPayment.PAYMENT_STATE_CANCELED
            payment.save(update_fields=["state"])
