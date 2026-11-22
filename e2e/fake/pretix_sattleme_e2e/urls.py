from django.http import HttpRequest, JsonResponse
from django.urls import re_path
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .fake import pay


def pay_invoice(request: HttpRequest) -> JsonResponse:
    """The test pays the invoice of a Lightning invoice (`bolt11`) the fake made."""
    ref = pay(request.POST.get("bolt11", ""))
    return JsonResponse({"ref": ref}, status=404 if ref is None else 200)


urlpatterns = [
    re_path(r"^_e2e/sattleme/pay$", csrf_exempt(require_POST(pay_invoice)), name="pay"),
]
