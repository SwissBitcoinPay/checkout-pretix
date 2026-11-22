from typing import Any

from pretix.base.models import Order, OrderPayment
from pretix.multidomain.urlreverse import eventreverse

# The tag of the order secrets in this plugin's links: a link of another plugin, or the order's
# own secret, never opens them.
SECRET_TAG = "plugins:pretix_sattleme"


def _kwargs(payment: OrderPayment) -> dict[str, Any]:
    return {
        "order": payment.order.code,
        "hash": payment.order.tagged_secret(SECRET_TAG),
        "payment": payment.pk,
    }


def pay_url(payment: OrderPayment) -> str:
    """The page where the customer pays the invoice of `payment`."""
    url: str = eventreverse(
        payment.order.event, "plugins:pretix_sattleme:pay", kwargs=_kwargs(payment)
    )
    return url


def status_url(payment: OrderPayment) -> str:
    """What that page asks, every few seconds, for news of the payment."""
    url: str = eventreverse(
        payment.order.event, "plugins:pretix_sattleme:status", kwargs=_kwargs(payment)
    )
    return url


def order_url(order: Order) -> str:
    """The order's page in the shop."""
    url: str = eventreverse(
        order.event,
        "presale:event.order",
        kwargs={"order": order.code, "secret": order.secret},
    )
    return url
