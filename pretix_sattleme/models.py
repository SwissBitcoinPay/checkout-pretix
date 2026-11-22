import time
from typing import TYPE_CHECKING, Any

from django.db import models
from django_scopes import ScopedManager

if TYPE_CHECKING:
    from pretix.base.models import OrderPayment

# The SDK may report a payment made in an invoice's last seconds up to this long after its end
# (the specification's grace period, in seconds).
GRACE_SECONDS = 120


class BitcoinInvoice(models.Model):
    """The Bitcoin invoice of one pretix payment, and what its checks found.

    Stored with its reference before the SDK is asked for an invoice of it, its invoice (`data`)
    once the SDK made it (invoices.py): a try that follows a failed one asks for the same
    reference again. A payment has one invoice at most: when it ends unpaid, the payment fails
    and the customer retries with a new payment, which gets a new reference.
    """

    payment = models.OneToOneField(
        "pretixbase.OrderPayment",
        on_delete=models.CASCADE,
        related_name="sattleme_invoice",
    )
    # The order reference the invoice is made for: random, never used for another payment.
    ref = models.CharField(max_length=64, unique=True)
    # The invoice as the SDK returned it (`Invoice.to_json()`, or the JSON of the earlier one it
    # handed out again): what every check is given. None until it is made.
    data = models.JSONField(null=True, blank=True)
    # What the last check found (`PaymentStatus.to_json()`), None before the first one.
    status = models.JSONField(null=True, blank=True)
    # Paid or expired, and the pretix payment updated to match: never checked again.
    final = models.BooleanField(default=False)
    created = models.DateTimeField(auto_now_add=True)
    # When a check last ended, successfully or not.
    checked = models.DateTimeField(null=True, blank=True)
    # When the background checks look at it next; None before it is made.
    next_check = models.DateTimeField(null=True, blank=True)
    # The chain of background checks allowed to check it: a chain that is no longer this one
    # stops (see checks.py).
    chain = models.CharField(max_length=32, blank=True)
    # A check under way holds the invoice until then, so that two never run at once.
    lease_until = models.DateTimeField(null=True, blank=True)
    # The code of the error of the last try to make it, or of its last check (display.py says it
    # in plain words), and how many checks in a row failed.
    error = models.CharField(max_length=64, blank=True)
    errors = models.PositiveIntegerField(default=0)
    # The event's personal data was removed (pretix's data shredder, « Payment information »):
    # what leads to the customer's coins is gone, or goes once the invoice is final (its checks
    # need it until then). See shredding.py.
    shredded = models.BooleanField(default=False)

    objects = ScopedManager(organizer="payment__order__event__organizer")

    class Meta:
        indexes = [models.Index(fields=["final", "next_check"])]

    def __str__(self) -> str:
        return str(self.ref)

    @property
    def made(self) -> bool:
        """Whether the SDK made its invoice (else only its reference is stored)."""
        return self.data is not None

    @property
    def sats(self) -> int:
        return int(self.data["sats"])

    @property
    def expires_at(self) -> int:
        return int(self.data["expiresAt"])

    @property
    def end(self) -> int:
        """When the SDK reports its payment as final at the latest (Unix seconds)."""
        return self.expires_at + GRACE_SECONDS

    @property
    def bolt11(self) -> str | None:
        value: str | None = self.data.get("bolt11")
        return value

    @property
    def payment_hash(self) -> str | None:
        value: str | None = self.data.get("paymentHash")
        return value

    @property
    def onchain_address(self) -> str | None:
        value: str | None = self.data.get("onchainAddress")
        return value

    @property
    def payment_uri(self) -> str:
        return str(self.data["paymentUri"])

    @property
    def state(self) -> str | None:
        """The state of the last status (open, unconfirmed, paid, expired), or None."""
        value: str | None = self.status["state"] if self.status else None
        return value

    @property
    def received(self) -> int:
        """What came in time, in sats (on-chain, and by invoices of the rest)."""
        if not self.status or self.status.get("received") is None:
            return 0
        return int(self.status["received"])

    @property
    def rest(self) -> dict[str, Any] | None:
        """What is left to pay of an on-chain underpayment, and how; None without one."""
        value: dict[str, Any] | None = self.status.get("rest") if self.status else None
        return value

    @property
    def confirmations(self) -> int | None:
        value: int | None = self.status.get("confirmations") if self.status else None
        return value

    def may_be_paid(self) -> bool:
        """Whether its invoice may still be paid, or a payment of it be confirmed: until its
        final status is known, and once final, while its payment awaits the organizer
        (`awaits_organizer`). An order has one such invoice at a time: its customer may choose
        another payment method, or get a new invoice, only once it ended unpaid.

        Its time being over is not enough: a payment of its last seconds, or an on-chain one
        only BlockCypher dates in time, is found by a check after its end (its grace period
        included); until one succeeds, the invoice may have been paid.
        """
        if not self.made:
            return False
        if self.final:
            return self.awaits_organizer(self.payment)
        return True

    def awaits_organizer(self, payment: "OrderPayment") -> bool:
        """Final, and its payment left pending for the organizer, who settles with the customer
        and confirms it by hand, or cancels it: found paid while the order owed another amount,
        as it changed after the invoice was made (checks._paid); ended underpaid, what came
        having reached the merchant's wallet (checks._expired); or given up on, its final
        status unknown after GIVE_UP_AFTER (checks._give_up). Only an invoice that ended unpaid
        fails its payment."""
        return self.final and payment.state in (
            payment.PAYMENT_STATE_CREATED,
            payment.PAYMENT_STATE_PENDING,
        )

    def info(self) -> dict[str, Any]:
        """What pretix keeps of the invoice in its payment's information (logs, exports)."""
        from . import shredding

        info: dict[str, Any] = {
            "ref": self.ref,
            "sats": self.data["sats"],
            "payment_hash": self.payment_hash,
            "onchain_address": self.onchain_address,
        }
        if self.status:
            info["status"] = self.status
        return shredding.shred_info(info) if self.shredded else info

    def phase(self, payment: "OrderPayment", now: int | None = None) -> str:
        """What the customer is told of this invoice and its payment.

        - ``paid``: pretix confirmed the payment;
        - ``arrived``: the SDK reported the invoice paid, and pretix's confirmation of the
          payment is under way (the check that saw it applies it, or a later one, should that
          fail): the customer is sent to the order only once pretix confirmed it;
        - ``review``: paid, the payment left to the organizer, who confirms it by hand: the
          order changed after the invoice was made (`awaits_organizer`);
        - ``expired``: never paid, for good (``received`` above 0: underpaid, the payment left
          to the organizer);
        - ``unconfirmed``: enough came on-chain, being confirmed;
        - ``canceled``: the pretix payment was canceled or failed;
        - ``ended``: its time is over, its final status not known yet;
        - ``underpaid``: less than the amount came on-chain, the rest may still be paid;
        - ``open``: waiting for the payment.
        """
        now = int(time.time()) if now is None else now
        if payment.state in (
            payment.PAYMENT_STATE_CONFIRMED,
            payment.PAYMENT_STATE_REFUNDED,
        ):
            return "paid"
        if self.state == "paid":
            if not self.final:
                return "arrived"
            return "review" if self.awaits_organizer(payment) else "canceled"
        if self.state == "expired":
            return "expired"
        if self.state == "unconfirmed":
            return "unconfirmed"
        if payment.state in (
            payment.PAYMENT_STATE_CANCELED,
            payment.PAYMENT_STATE_FAILED,
        ):
            return "canceled"
        if now >= self.expires_at:
            return "ended"
        if self.received > 0:
            return "underpaid"
        return "open"
