import re
import time
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils.timezone import now
from django_scopes import scopes_disabled
from pretix.base.models import Order, OrderPayment
from pretix.base.payment import PaymentException
from sattleme_checkout import (
    ChannelNotFoundError,
    InvalidInvoiceError,
    InvalidOrderError,
    InvoicePendingError,
    NetworkError,
    OnchainUnavailableError,
    OrderPaidError,
    PaymentStatus,
    Price,
    RateError,
    Rest,
    RevokedError,
    UnsupportedCurrencyError,
)

from pretix_sattleme import checks, invoices, sdk
from pretix_sattleme.models import BitcoinInvoice

from .conftest import declared, make_invoice

PREIMAGE = "cd" * 32


def duration(provider, order):
    return invoices.invoice_duration(provider, invoices.payment_deadline(order))


@pytest.mark.django_db
def test_create_makes_one_invoice(fake_sdk, provider, payment):
    with scopes_disabled():
        row = invoices.create(provider, payment)

        assert len(fake_sdk.created) == 1
        call = fake_sdk.created[0]
        assert call["fiat"] == Price("24.50", "CHF")
        assert call["duration_seconds"] == invoices.DEFAULT_MINUTES * 60 == 900
        assert call["note"] == "Conference · ABC12"
        assert call["timeout"] == sdk.CREATE_TIMEOUT
        assert "sats" not in call and "checkout" not in call
        assert re.fullmatch(r"pretix-ABC12-1-[0-9a-f]{32}", call["ref"])
        assert fake_sdk.options == [
            {
                "relay_timeout_ms": sdk.RELAY_TIMEOUT_MS,
                "http_timeout_ms": sdk.HTTP_TIMEOUT_MS,
            }
        ]

        assert row.ref == call["ref"]
        assert row.data["ref"] == row.ref
        assert row.chain and not row.final
        payment.refresh_from_db()
        assert payment.state == OrderPayment.PAYMENT_STATE_PENDING
        assert payment.info_data["ref"] == row.ref
        [entry] = payment.order.all_logentries().filter(
            action_type="pretix_sattleme.invoice.created"
        )
        # Its amount grouped, its payment named: the reference, too long for the order's
        # history, is in the payment's details.
        assert entry.parsed_data["ref"] == row.ref
        assert (
            str(entry.display())
            == "A Bitcoin invoice of 49,000 sats was created for payment 1."
        )

        # Once more (a double click, a reload): the same invoice, nothing asked again.
        assert invoices.create(provider, payment) == row
        assert len(fake_sdk.created) == 1


@pytest.mark.django_db
def test_refs_never_repeat(payment):
    # 128 random bits each, after the order's code and the payment's number.
    refs = {invoices.new_ref(payment) for _ in range(100)}
    assert len(refs) == 100
    assert all(re.fullmatch(r"pretix-ABC12-1-[0-9a-f]{32}", ref) for ref in refs)


@pytest.mark.django_db
def test_a_ref_always_fits_the_sdk(payment):
    # At most 64 of A-Z a-z 0-9 . _ : -, whatever the order's code and the payment's number.
    payment.order.code = "ÄBC" + "D" * 30
    payment.local_id = 7
    assert re.fullmatch(r"pretix-BCD{14}-7-[0-9a-f]{32}", invoices.new_ref(payment))
    payment.local_id = 10**9
    assert re.fullmatch(r"pretix-[0-9a-f]{32}", invoices.new_ref(payment))


@pytest.mark.django_db
def test_the_reference_is_stored_before_the_invoice_is_asked(
    fake_sdk, provider, payment, monkeypatch
):
    from .conftest import FakeClient

    stored = []
    create_invoice = FakeClient.create_invoice

    def asked(self, **kwargs):
        rows = BitcoinInvoice.objects.filter(payment=payment, ref=kwargs["ref"])
        stored.append([row.made for row in rows])
        return create_invoice(self, **kwargs)

    monkeypatch.setattr(FakeClient, "create_invoice", asked)
    with scopes_disabled():
        row = invoices.create(provider, payment)
    assert stored == [[False]] and row.made


@pytest.mark.django_db
def test_a_try_after_a_failure_asks_for_the_same_reference(fake_sdk, provider, payment):
    fake_sdk.invoice = NetworkError("no relay")
    with scopes_disabled(), pytest.raises(PaymentException, match="try again"):
        invoices.create(provider, payment)
    with scopes_disabled():
        row = BitcoinInvoice.objects.get(payment=payment)
    assert not row.made and row.next_check is None and row.error == "network"
    # Nothing to check, nothing to sweep.
    assert not checks.check(row.pk)
    checks.sweep()
    assert fake_sdk.checked == []

    fake_sdk.invoice = None
    with scopes_disabled():
        made = invoices.create(provider, payment)
    assert [call["ref"] for call in fake_sdk.created] == [row.ref, row.ref]
    assert made.pk == row.pk and made.made and made.error == ""


@pytest.mark.django_db
def test_an_earlier_invoice_is_shown_again(fake_sdk, provider, payment):
    """An earlier try of the payment failed once the SDK had made its invoice: the SDK hands
    that one out again, never a second one."""
    with scopes_disabled():
        ref = invoices.record(payment).ref
    earlier = declared(make_invoice(ref=ref, onchain=True))
    fake_sdk.invoice = InvoicePendingError(
        "pending", invoice=earlier, checkout_url=None
    )
    with scopes_disabled():
        row = invoices.create(provider, payment)
        payment.refresh_from_db()
        [entry] = payment.order.all_logentries().filter(
            action_type="pretix_sattleme.invoice.recovered"
        )
    assert row.ref == ref and row.data == earlier.to_json() and row.chain
    assert payment.state == OrderPayment.PAYMENT_STATE_PENDING
    assert payment.info_data["onchain_address"] == earlier.onchain_address
    assert str(entry.display()) == (
        "The Bitcoin invoice of 49,000 sats made earlier for payment 1 was found again "
        "in the merchant's registry, and shown again."
    )
    # Checked as any other: what is stored is the SDK's JSON.
    fake_sdk.statuses = [PaymentStatus.open(earlier.expires_at, 0, None)]
    assert checks.check(row.pk)
    assert fake_sdk.checked[-1][0] == earlier.to_json()


@pytest.mark.django_db
def test_an_earlier_invoice_with_a_rest_is_shown_with_it(fake_sdk, provider, payment):
    with scopes_disabled():
        ref = invoices.record(payment).ref
    earlier = declared(make_invoice(ref=ref, onchain=True))
    fake_sdk.invoice = InvoicePendingError(
        "pending", invoice=earlier, checkout_url=None
    )
    with scopes_disabled():
        row = invoices.create(provider, payment)
    rest = Rest(sats=19_000, bolt11="lnbc190u1rest", payment_uri="bitcoin:x")
    fake_sdk.statuses = [PaymentStatus.open(earlier.expires_at, 30_000, rest)]
    checks.check(row.pk)
    with scopes_disabled():
        row = BitcoinInvoice.objects.get(pk=row.pk)
    assert row.received == 30_000 and row.rest["sats"] == "19000"


@pytest.mark.parametrize("workers", [True, False], ids=["celery", "no-workers"])
@pytest.mark.django_db
def test_an_earlier_invoice_paid_confirms_the_payment(
    fake_sdk,
    provider,
    payment,
    order,
    monkeypatch,
    workers,
    django_capture_on_commit_callbacks,
):
    """The invoice of the payment's reference was paid (this server lost it: a database
    restored): stored, then confirmed by the plugin's own check, at once in the background.
    Never in the request, which may have waited for the SDK for all its time already
    (gunicorn stops a worker after 30 s by default)."""
    dispatched = []
    monkeypatch.setattr(checks, "workers", lambda: workers)
    monkeypatch.setattr(checks, "_dispatch", lambda *args: dispatched.append(args))
    with scopes_disabled():
        ref = invoices.record(payment).ref
    earlier = declared(make_invoice(ref=ref))
    status = PaymentStatus.paid_lightning(PREIMAGE)
    fake_sdk.invoice = OrderPaidError(
        "paid", invoice=earlier, status=status, preimage=PREIMAGE
    )
    fake_sdk.statuses = [status]
    with scopes_disabled(), django_capture_on_commit_callbacks(execute=True):
        row = invoices.create(provider, payment)
    with scopes_disabled():
        payment.refresh_from_db()
    assert fake_sdk.checked == []
    assert row.made and not row.final and row.state == "paid"
    assert payment.state == OrderPayment.PAYMENT_STATE_PENDING
    # The customer's page says the payment arrived: no QR code of a paid invoice.
    assert row.phase(payment) == "arrived"
    # Its chain's first check is due now: dispatched to Celery's workers once committed;
    # without workers, run by the page's status endpoint or pretix's periodic tasks.
    assert row.next_check <= now()
    assert dispatched == ([(order.event_id, row.pk, row.chain, 0)] if workers else [])

    checks.run_chain(row.pk, row.chain)
    with scopes_disabled():
        row = BitcoinInvoice.objects.select_related("payment").get(pk=row.pk)
    order.refresh_from_db()
    assert row.final and row.state == "paid" and len(fake_sdk.checked) == 1
    assert row.payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED
    assert order.status == Order.STATUS_PAID


@pytest.mark.django_db
def test_an_earlier_invoice_paid_waits_for_the_plugins_own_check(
    fake_sdk, provider, payment, order
):
    """Never confirmed on the error's word alone: its check failing, the payment stays pending
    and the background checks go on."""
    with scopes_disabled():
        ref = invoices.record(payment).ref
    earlier = declared(make_invoice(ref=ref))
    fake_sdk.invoice = OrderPaidError(
        "paid",
        invoice=earlier,
        status=PaymentStatus.paid_lightning(PREIMAGE),
        preimage=PREIMAGE,
    )
    fake_sdk.statuses = [NetworkError("no relay")]
    with scopes_disabled():
        row = invoices.create(provider, payment)
    checks.check(row.pk)
    with scopes_disabled():
        row = BitcoinInvoice.objects.select_related("payment").get(pk=row.pk)
    order.refresh_from_db()
    assert row.made and not row.final and row.error == "network" and row.chain
    assert row.payment.state == OrderPayment.PAYMENT_STATE_PENDING
    assert order.status == Order.STATUS_PENDING


@pytest.mark.django_db
def test_an_invoice_of_another_price_is_never_shown(fake_sdk, provider, payment):
    fake_sdk.invoice = InvoicePendingError(
        "pending",
        invoice=declared(make_invoice(fiat=Price("10.00", "CHF"))),
        checkout_url=None,
    )
    with (
        scopes_disabled(),
        pytest.raises(PaymentException, match="not available right now"),
    ):
        invoices.create(provider, payment)
    with scopes_disabled():
        row = BitcoinInvoice.objects.get(payment=payment)
        entry = payment.order.all_logentries().get(
            action_type="pretix_sattleme.invoice.failed"
        )
        payment.refresh_from_db()
    assert not row.made and row.error == "mismatch"
    assert entry.parsed_data["error"] == "mismatch"
    assert payment.state == OrderPayment.PAYMENT_STATE_CREATED


@pytest.mark.django_db
def test_an_order_has_one_payable_invoice_at_a_time(
    fake_sdk, provider, invoice_row, order
):
    """Another payment of the order (an order changed meanwhile, two tabs): no second invoice
    while the first one may still be paid, a Lightning invoice being irrevocable."""
    with scopes_disabled():
        second = order.payments.create(
            provider="sattleme", state="created", amount=order.total
        )
        with pytest.raises(PaymentException, match="can still be paid, until"):
            invoices.create(provider, second)
        # Something came: none either, until the final status is known.
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(
            status=PaymentStatus.open(invoice_row.expires_at, 30_000, None).to_json()
        )
        with pytest.raises(PaymentException, match="arrived and is being checked"):
            invoices.create(provider, second)
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(
            status=PaymentStatus.unconfirmed(49_000, 1, ("ef" * 32,)).to_json()
        )
        with pytest.raises(PaymentException, match="arrived and is being checked"):
            invoices.create(provider, second)
        assert fake_sdk.created == []

        # Its end passed, nothing came yet: none until a check after it says so.
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(
            status=None,
            data=make_invoice(created=int(now().timestamp()) - 2000).to_json(),
        )
        with pytest.raises(PaymentException, match="known to be unpaid"):
            invoices.create(provider, second)
        assert fake_sdk.created == []

        # Expired, nothing received (its payment failed, as the check that found it does):
        # the order may get a new one.
        BitcoinInvoice.objects.filter(pk=invoice_row.pk).update(
            status=PaymentStatus.expired(0).to_json(), final=True
        )
        OrderPayment.objects.filter(pk=invoice_row.payment_id).update(state="failed")
        row = invoices.create(provider, second)
    assert row.made and len(fake_sdk.created) == 1


@pytest.mark.django_db
def test_amounts_are_written_plainly(fake_sdk, provider, payment):
    payment.amount = Decimal("1000.00")
    payment.save()
    with scopes_disabled():
        invoices.create(provider, payment)
    assert fake_sdk.created[0]["fiat"] == Price("1000.00", "CHF")


@pytest.mark.parametrize(
    "error, message, log",
    [
        (NetworkError("no relay"), "try again in a moment", "network"),
        (TimeoutError(), "try again in a moment", "timeout"),
        (RateError("too old"), "try again in a moment", "rate"),
        (
            OnchainUnavailableError("lot empty"),
            "try again in a moment",
            "onchain_unavailable",
        ),
        (
            InvalidOrderError("fiat: too small", param="fiat"),
            "cannot be paid with Bitcoin",
            "invalid_order",
        ),
        (
            UnsupportedCurrencyError("no rate", currency="CHF"),
            "cannot be paid with Bitcoin",
            "unsupported_currency",
        ),
        (RevokedError("removed"), "not available right now", "revoked"),
        (
            ChannelNotFoundError("no channel", newer_format=False),
            "not available right now",
            "channel_not_found",
        ),
        (
            ChannelNotFoundError("newer", newer_format=True),
            "not available right now",
            "channel_not_found",
        ),
        (
            InvalidInvoiceError("bad pr", check="amount"),
            "not available right now",
            "invalid_invoice",
        ),
        (RuntimeError("a bug"), "not available right now", "error"),
    ],
)
@pytest.mark.django_db
def test_failures_show_nothing(fake_sdk, provider, payment, error, message, log):
    fake_sdk.invoice = error
    with scopes_disabled(), pytest.raises(PaymentException, match=message):
        invoices.create(provider, payment)
    with scopes_disabled():
        # Its reference stays, for the next try; no invoice to show, nor to check.
        [row] = BitcoinInvoice.objects.all()
        assert not row.made and row.error == log and row.next_check is None
        payment.refresh_from_db()
        assert payment.state == OrderPayment.PAYMENT_STATE_CREATED
        entry = payment.order.all_logentries().get(
            action_type="pretix_sattleme.invoice.failed"
        )
    assert entry.parsed_data == {"local_id": 1, "error": log}


@pytest.mark.parametrize("key", [None, "sattleme_ck_nope"])
@pytest.mark.django_db
def test_a_missing_or_invalid_key_is_a_configuration_error(
    fake_sdk, provider, payment, event, key
):
    """The real SDK refuses the key (InvalidKeyError) before any request."""
    if key is None:
        event.settings.delete("payment_sattleme_key")
    else:
        event.settings.set("payment_sattleme_key", key)
    with (
        scopes_disabled(),
        pytest.raises(PaymentException, match="not available right now"),
    ):
        invoices.create(provider, payment)
    assert fake_sdk.created == []
    with scopes_disabled():
        [row] = BitcoinInvoice.objects.all()
        assert not row.made and row.error == "invalid_key"


@pytest.mark.django_db
def test_no_invoice_for_a_payment_no_longer_open(fake_sdk, provider, payment):
    payment.state = OrderPayment.PAYMENT_STATE_CANCELED
    payment.save()
    with scopes_disabled(), pytest.raises(PaymentException, match="no longer"):
        invoices.create(provider, payment)
    assert fake_sdk.created == []


@pytest.mark.django_db
def test_an_invoice_past_the_deadline_is_never_shown(
    fake_sdk, provider, payment, order
):
    """The merchant's Lightning address dates it later than asked (its clock is ahead)."""
    order.expires = now() + timedelta(minutes=10)
    order.save()
    fake_sdk.invoice = make_invoice(created=int(now().timestamp()) + 200, duration=480)
    with scopes_disabled(), pytest.raises(PaymentException, match="almost over"):
        invoices.create(provider, payment)
    with scopes_disabled():
        assert not BitcoinInvoice.objects.get().made
        assert (
            payment.order.all_logentries()
            .get(action_type="pretix_sattleme.invoice.failed")
            .parsed_data["error"]
            == "clock"
        )


@pytest.mark.django_db
def test_an_invoice_made_slowly_near_the_deadline_is_shown(
    fake_sdk, provider, payment, order, monkeypatch
):
    """The SDK dates the invoice when it makes it, a few seconds after its duration was set:
    it still ends, with its grace period, by the deadline."""
    from .conftest import FakeClient

    order.expires = now() + timedelta(minutes=10)
    order.save()

    def slow_create(self, **kwargs):
        self.sdk.created.append(kwargs)
        return make_invoice(
            ref=kwargs["ref"],
            duration=kwargs["duration_seconds"],
            created=int(now().timestamp()) + 5,
        )

    monkeypatch.setattr(FakeClient, "create_invoice", slow_create)
    with scopes_disabled():
        row = invoices.create(provider, payment)
    assert row.end <= order.expires.timestamp()


@pytest.mark.django_db
def test_an_invoice_ends_minutes_before_the_order_expires(
    fake_sdk, provider, payment, order, event, monkeypatch
):
    """Its checks after its end come before pretix expires the order: an on-chain payment of
    its grace period's last seconds (one only BlockCypher dates in time), seen by the first
    of them, keeps the order from expiring. Even when the SDK took all of its time to make it
    (the invoice is dated from then)."""
    from . import conftest

    event.settings.set("payment_term_expire_automatically", True)
    order.expires = now() + timedelta(minutes=10)
    order.save()
    made = conftest.make_invoice
    monkeypatch.setattr(
        conftest,
        "make_invoice",
        lambda **kwargs: made(created=int(time.time()) + sdk.CREATE_TIMEOUT, **kwargs),
    )
    with scopes_disabled():
        row = invoices.create(provider, payment)
        deadline = order.payment_term_expire_date.timestamp()
    assert row.made
    assert deadline - row.end >= invoices.DEADLINE_MARGIN
    assert row.end + checks.END_MARGIN + checks.OPEN_INTERVAL < deadline

    # Less time than that: no invoice.
    order.expires = now() + timedelta(seconds=240)
    order.save()
    with scopes_disabled():
        second = order.payments.create(
            provider="sattleme", state="created", amount=order.total
        )
        BitcoinInvoice.objects.filter(pk=row.pk).update(
            status=PaymentStatus.expired(0).to_json(), final=True
        )
        OrderPayment.objects.filter(pk=row.payment_id).update(state="failed")
        with pytest.raises(PaymentException, match="almost over"):
            invoices.create(provider, second)


@pytest.mark.django_db
def test_no_second_call_after_waiting_for_another_request(
    fake_sdk, provider, payment, monkeypatch
):
    clock = iter([100.0, 100.0 + invoices.LOCK_WAIT + 1])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    with scopes_disabled(), pytest.raises(PaymentException, match="try again"):
        invoices.create(provider, payment)
    assert fake_sdk.created == []


@pytest.mark.django_db
def test_duration_setting(fake_sdk, provider, payment, event):
    event.settings.set("payment_sattleme_invoice_minutes", 30)
    with scopes_disabled():
        invoices.create(provider, payment)
    assert fake_sdk.created[0]["duration_seconds"] == 1800


@pytest.mark.django_db
def test_duration_ends_with_the_last_date_of_payments(
    provider, order, event, monkeypatch
):
    deadline = now() + timedelta(minutes=10)
    monkeypatch.setattr(
        type(order), "payment_term_last", property(lambda self: deadline)
    )
    # It ends a grace period before (a payment the SDK may still count comes in time), and a
    # margin more (its last checks), from the moment the SDK may date it.
    assert 150 <= duration(provider, order) <= 160

    monkeypatch.setattr(
        type(order),
        "payment_term_last",
        property(lambda self: now() + timedelta(seconds=490)),
    )
    with pytest.raises(PaymentException, match="almost over"):
        duration(provider, order)


@pytest.mark.django_db
def test_duration_ends_with_the_order(provider, order, event):
    # Before the order expires (its products are freed then), whatever pretix's settings.
    order.expires = now() + timedelta(minutes=9)
    order.save()
    assert 90 <= duration(provider, order) <= 100

    # An order already past its expiry: a late payment when pretix accepts them...
    order.expires = now() - timedelta(minutes=1)
    order.save()
    assert duration(provider, order) == 900
    # ... else none.
    event.settings.set("payment_term_accept_late", False)
    with pytest.raises(PaymentException, match="almost over"):
        duration(provider, order)

    # An order that never expires.
    order.valid_if_pending = True
    order.save()
    assert duration(provider, order) == 900
    order.valid_if_pending = False
    event.settings.set("payment_term_expire_automatically", False)
    assert duration(provider, order) == 900


@pytest.mark.django_db
def test_note(order, event):
    assert invoices.invoice_note(order) == "Conference · ABC12"

    event.name = "A\tvery\nlong " + "name " * 40
    event.save()
    note = invoices.invoice_note(order)
    assert len(note) == 100
    assert note.endswith("… · ABC12")
    assert note.startswith("A very long name")
    assert not any(ord(c) < 0x20 for c in note)

    event.name = "\n"
    event.save()
    assert invoices.invoice_note(order) == "ABC12"


@pytest.mark.django_db
def test_invoice_json_reads_back(payment):
    """What is stored is the specification's JSON: the SDK reads it back as it wrote it."""
    from sattleme_checkout import Invoice

    invoice = make_invoice(onchain=True)
    with scopes_disabled():
        row = BitcoinInvoice.objects.create(
            payment=payment, ref=invoice.ref, data=invoice.to_json(), next_check=now()
        )
        row = BitcoinInvoice.objects.get(pk=row.pk)
    assert Invoice.from_json(row.data) == invoice


def run_at_once(*requests):
    """Run each request in a thread of its own, at once; its result or its exception."""
    import threading

    from django.db import connections

    outcomes: list[object] = [None] * len(requests)

    def run(index, request):
        try:
            with scopes_disabled():
                outcomes[index] = request()
        except Exception as e:
            outcomes[index] = e
        finally:
            connections.close_all()

    threads = [
        threading.Thread(target=run, args=(i, r)) for i, r in enumerate(requests)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not any(thread.is_alive() for thread in threads)
    return outcomes


@pytest.fixture
def held_until_another_waits(fake_sdk, monkeypatch):
    """The first request to ask for an invoice holds its locks until PostgreSQL shows another
    request waiting for a lock: whatever the speed of the machine, the second request comes
    while the first one makes the invoice."""
    from django.db import connections

    from .conftest import FakeClient

    create_invoice = FakeClient.create_invoice

    def wait_for_a_waiter():
        observer = connections.create_connection("default")
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                with observer.cursor() as cursor:
                    cursor.execute("SELECT count(*) FROM pg_locks WHERE NOT granted")
                    if cursor.fetchone()[0]:
                        return
                time.sleep(0.05)
            raise AssertionError("no other request ever waited for a lock")
        finally:
            observer.close()

    def held(self, **kwargs):
        if len(self.sdk.created) == 0:
            wait_for_a_waiter()
        return create_invoice(self, **kwargs)

    monkeypatch.setattr(FakeClient, "create_invoice", held)
    return fake_sdk


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True)
def test_two_requests_at_once_make_one_invoice(
    held_until_another_waits, provider, payment
):
    """Two requests for one payment at the same time (a double click, two workers): the second
    one waits for the payment's lock, then gets the first one's invoice."""

    def request():
        return invoices.create(provider, OrderPayment.objects.get(pk=payment.pk)).pk

    first, second = run_at_once(request, request)
    assert isinstance(first, int) and first == second
    assert len(held_until_another_waits.created) == 1


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True)
def test_two_payments_of_an_order_at_once_make_one_invoice(
    held_until_another_waits, provider, order
):
    """Two payments of one order at the same time: the order is locked around the making of
    an invoice, and the second one finds the first one's invoice, which may still be paid.
    """
    with scopes_disabled():
        payments = [
            order.payments.create(
                provider="sattleme", state="created", amount=order.total
            )
            for _ in range(2)
        ]

    def request(pk):
        return lambda: invoices.create(provider, OrderPayment.objects.get(pk=pk)).pk

    outcomes = run_at_once(*(request(p.pk) for p in payments))
    made = [o for o in outcomes if isinstance(o, int)]
    refused = [o for o in outcomes if isinstance(o, PaymentException)]
    assert len(made) == 1 and len(refused) == 1, outcomes
    assert "can still be paid" in str(refused[0])
    assert len(held_until_another_waits.created) == 1
