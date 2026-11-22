# Changelog

Versions of `pretix-sattleme` ([semantic versioning](https://semver.org)).

## 1.0.0 - 2026-11-22

First version, on the sattle.me checkout SDK 1.0.0 (`sattleme-checkout`, any 1.x release), for
pretix 2026.7 and later.

- The payment method **Bitcoin (sattle.me)**: its integration key, checked when saved and never
  shown again, how long an invoice can be paid, and its name and description at checkout.
- The method offered only for a price the SDK takes: in a currency of its table, for an amount of
  at most the decimals it prices that currency with (the order's total with the method's fee, or
  what the order owes when its customer changes to it). Its settings page says when the event's
  currency cannot be used, or only for amounts in whole units.
- An invoice per payment, made once the order is confirmed, for a random reference; the payment
  locked while it is made, so that two requests at once get one invoice.
- A payment page in the shop: the QR code (made on the server), the invoice and the address to
  copy, the time left, the rest of an on-chain underpayment, the confirmations; updated on its
  own, usable without JavaScript, and in pretix's widget too.
- Payments checked for the payment page, by a chain of Celery tasks per invoice (with a check just
  before the end of its grace period) and by pretix's periodic tasks; confirmed on a `paid` status
  only, failed when expired unpaid, left pending for the organizer with a note when expired
  underpaid; an order whose confirmed payment pretix failed to mark paid marked paid by the next
  check.
- On-chain payments paid at 3 confirmations on the explorer the SDK reads: mempool.space, or the
  merchant's own Bitcoin node when the merchant's app names one, with its fallback.
- Invoices that end 5 minutes before pretix's deadlines (the order's last date of payments and
  expiry), payments made in time confirmed however long their confirmations take, and an order
  kept from expiring while its on-chain payment is being confirmed (brought back when pretix
  expired it before a check saw that payment, its products allowing). Another payment method, or
  a new invoice, only once an invoice ended unpaid, as a check after its end finds it: an order
  is never paid twice by its customer.
- An order whose total changed after its invoice was made (changed, canceled or paid otherwise),
  then paid, never completed automatically: the payment stays pending, with a note of the amount
  received and what the order owes now, for the organizer to confirm by hand. A payment of
  another amount in sats than the invoice's is noted too: an overpayment, or less that the
  merchant accepted in the sattle.me app (its tolerance, or by hand what an order underpaid
  on-chain received), confirmed as paid.
- pretix's data shredder « Payment information » also removes what leads to the customer's coins:
  the deposit address and the transactions, from the payments' information, from pretix's log
  entries of them, and from their invoices.
- The payment's details in pretix's backend and REST API, the order's history; translations in
  German (formal and informal), French and Italian.
