from pretix.base.models import Event
from pretix.base.services.tasks import EventTask
from pretix.celery_app import app

from . import checks


def _check_invoice(event: Event, invoice_id: int, chain: str | None = None) -> None:
    """Check the payment of an invoice: one step of its background checks (`chain`), or a
    check asked by its customer's page (no `chain`)."""
    if chain:
        checks.run_chain(invoice_id, chain)
    else:
        checks.check(invoice_id, min_interval=checks.WEB_INTERVAL)


# Registered without a decorator, under its own name: Celery's is untyped, and would hide the
# function's types from mypy.
check_invoice = app.task(
    _check_invoice,
    base=EventTask,
    ignore_result=True,
    name="pretix_sattleme.tasks.check_invoice",
)
