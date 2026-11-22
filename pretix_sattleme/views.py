import hashlib
import json
from functools import cached_property
from typing import Any

from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.generic import TemplateView
from pretix.base.models import Order
from pretix.presale.views import EventViewMixin
from pretix.presale.views.robots import NoSearchIndexViewMixin

from . import checks, display, links
from .models import BitcoinInvoice
from .payment import Sattleme, invoice_of


def version(invoice: BitcoinInvoice, phase: str) -> str:
    """What the payment page shows, in a few characters: the page reloads when it changes."""
    rest = invoice.rest or {}
    shown = [
        phase,
        invoice.received,
        rest.get("bolt11"),
        rest.get("paymentUri"),
        invoice.confirmations,
    ]
    return hashlib.sha256(json.dumps(shown).encode()).hexdigest()[:16]


class PaymentMixin(NoSearchIndexViewMixin):
    """The payment of the link, found by the order's code, a secret of the order tagged for
    this plugin, and the payment's id: a payment of this provider, of that order only.
    """

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        try:
            self.order = request.event.orders.get_with_secret_check(
                code=kwargs["order"],
                received_secret=kwargs["hash"],
                tag=links.SECRET_TAG,
            )
        except Order.DoesNotExist:
            raise Http404("Unknown order")
        self.payment = get_object_or_404(
            self.order.payments,
            pk=kwargs["payment"],
            provider=Sattleme.identifier,
        )
        response: HttpResponse = super().dispatch(request, *args, **kwargs)
        return response

    @cached_property
    def invoice(self) -> BitcoinInvoice | None:
        # Through the payment: the invoice's payment is then this one, with its order.
        return invoice_of(self.payment)

    def order_url(self, paid: bool = False) -> str:
        return links.order_url(self.order) + ("?paid=yes" if paid else "")


@method_decorator(xframe_options_exempt, "dispatch")
@method_decorator(never_cache, "dispatch")
class PayView(PaymentMixin, EventViewMixin, TemplateView):
    """The page where the customer pays: the invoice's QR code while it can be paid, then
    what became of the payment. It shows what the checks found and runs no check itself,
    but for a customer without JavaScript who asks for one (`?check=1`)."""

    template_name = "pretix_sattleme/pay.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        invoice = self.invoice
        if invoice is None:
            response: HttpResponse = redirect(self.order_url())
        elif request.GET.get("check"):
            checks.check(
                invoice.pk, min_interval=checks.WEB_INTERVAL, timeout=checks.WEB_TIMEOUT
            )
            response = redirect(links.pay_url(self.payment))
        elif invoice.phase(self.payment) == "paid":
            response = redirect(self.order_url(paid=True))
        else:
            response = super().get(request, *args, **kwargs)
        return response

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        invoice = self.invoice
        if invoice is None:  # get() renders the page only with one.
            raise Http404("No invoice")
        context: dict[str, Any] = super().get_context_data(**kwargs)
        context.update(display.context(invoice, self.payment))
        context["version"] = version(invoice, context["phase"])
        context["status_url"] = links.status_url(self.payment)
        context["check_url"] = links.pay_url(self.payment) + "?check=1"
        context["order_url"] = self.order_url()
        return context


@method_decorator(xframe_options_exempt, "dispatch")
@method_decorator(never_cache, "dispatch")
class StatusView(PaymentMixin, View):
    """Asked by the payment page every few seconds: has the invoice checked soon, and says
    where its payment is (the page reloads when what it shows changed)."""

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        invoice = self.invoice
        if invoice is None:
            response: HttpResponse = JsonResponse(
                {"phase": "none", "redirect": self.order_url()}
            )
            return response
        checks.request_check(invoice)
        self.payment.refresh_from_db()
        invoice.refresh_from_db()
        phase = invoice.phase(self.payment)
        answer = {"phase": phase, "version": version(invoice, phase)}
        if phase == "paid":
            answer["redirect"] = self.order_url(paid=True)
        response = JsonResponse(answer)
        return response
