"""The fake of the SDK's client: the calls the plugin makes, answered without any network.

The real SDK still checks the key and the options of every client, as in production. Invoices
are of the price asked (one bitcoin at RATE), payable in Lightning, and `open` until the test
pays one (`pay`), then `paid`. Their state is a file of pretix's data directory, which every
process of pretix shares (its web workers, its task workers).
"""

import fcntl
import json
import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.conf import settings
from sattleme_checkout import (
    Client,
    Fiat,
    Invoice,
    Merchant,
    PaymentStatus,
    Price,
    RateInfo,
)

# One bitcoin, in any currency; the proof of payment of every paid invoice.
RATE = Decimal("50000")
PREIMAGE = "e2e0" * 16
DIGITS = 2


def _path() -> Path:
    return Path(settings.DATA_DIR) / "pretix-sattleme-e2e.json"


@contextmanager
def _state() -> Iterator[dict[str, Any]]:
    """The fake's state, locked, read, and written back when the block ends."""
    path = _path()
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {"invoices": {}, "paid": []}
        yield state
        path.write_text(json.dumps(state), encoding="utf-8")


def pay(bolt11: str) -> str | None:
    """Pay the invoice of `bolt11`: its reference, or None when the fake made none such."""
    with _state() as state:
        ref: str | None = state["invoices"].get(bolt11)
        if ref is not None and ref not in state["paid"]:
            state["paid"].append(ref)
    return ref


class FakeClient:
    def __init__(self, key: str, **options: Any) -> None:
        # The real SDK refuses a key that is not an integration key, or an invalid option.
        Client(key, **options)

    def __repr__(self) -> str:
        return "<Client>"

    def merchant(self, *, timeout: float | None = None) -> Merchant:
        return Merchant(
            business_name="End-to-end test",
            min_sats=1,
            max_sats=100_000_000,
            onchain_addresses=0,
        )

    def create_invoice(
        self,
        *,
        ref: str,
        fiat: Price,
        duration_seconds: int = 900,
        note: str | None = None,
        timeout: float | None = None,
    ) -> Invoice:
        minor = int((Decimal(fiat.amount) * 10**DIGITS).to_integral_exact())
        sats = math.ceil(Decimal(minor) * 100_000_000 / (RATE * 10**DIGITS))
        now = int(time.time())
        with _state() as state:
            bolt11 = f"lnbc{sats * 10}n1e2e{len(state['invoices']):08d}" + "q" * 200
            state["invoices"][bolt11] = ref
        return Invoice(
            ref=ref,
            bolt11=bolt11,
            payment_hash="e2e1" * 16,
            verify_url=None,
            onchain_address=None,
            payment_uri=f"lightning:{bolt11}",
            sats=sats,
            fiat=Fiat(
                amount=f"{minor // 10**DIGITS}.{minor % 10**DIGITS:0{DIGITS}d}",
                currency=fiat.currency,
                minor=minor,
                digits=DIGITS,
            ),
            rate=RateInfo(
                source="e2e",
                currency=fiat.currency,
                price=str(RATE),
                fetched_at=now,
                time=now,
            ),
            note=note or ref,
            created_at=now,
            expires_at=now + duration_seconds,
            checkout_url=None,
            redirect_url=None,
        )

    def get_status(
        self, invoice: dict[str, Any], *, timeout: float | None = None
    ) -> PaymentStatus:
        with _state() as state:
            paid = invoice["ref"] in state["paid"]
        if paid:
            return PaymentStatus.paid_lightning(PREIMAGE)
        return PaymentStatus.open(int(invoice["expiresAt"]), 0, None)
