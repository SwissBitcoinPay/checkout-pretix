# sattle.me for pretix

A payment plugin for [pretix](https://pretix.eu): customers pay their orders in bitcoin, in
Lightning or on-chain, and the bitcoin goes straight to the organizer's
[sattle.me](https://sattle.me) wallet. It is built on the
[sattle.me checkout SDK](https://pypi.org/project/sattleme-checkout/)
([source](https://github.com/sattleme/checkout-python)), which makes and checks the invoices:
no sattle.me server, no API key of a third party, and the integration key can make invoices that
pay the organizer, never spend the funds.

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Payments and orders](#payments-and-orders)
- [Background checks](#background-checks)
- [Security](#security)
- [Privacy](#privacy)
- [Known limits](#known-limits)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Releases](#releases)
- [License](#license)

## How it works

1. At checkout, the customer chooses **Bitcoin** (or the name the organizer gives the method).
   Once they confirm their order, the plugin asks the SDK for the invoice of the payment: a
   Lightning invoice, a deposit address of the organizer's wallet, or both, depending on the
   amount (see [the SDK](https://github.com/sattleme/checkout-python#on-chain-payments)).
2. The customer lands on a payment page of the shop, in its design and language: one QR code of
   the invoice (BIP-21 with `lightning=`, or `lightning:`), a button that opens their wallet with
   it, the Lightning invoice and the address to copy, the amount (in bitcoin, in sats and in the
   order's currency), and the time left to pay.
3. The page follows the payment on its own. Once the SDK reports it **paid**, the plugin confirms
   the pretix payment: the order is paid, the tickets are sent as usual, and the page takes the
   customer to the order.

The page is rendered on the server (the QR code is an inline SVG); its small script, a file of the
plugin, only counts down, copies, and reloads the page when the payment moved on. Without
JavaScript, the page offers a button to check the payment.

In [pretix's widget](https://docs.pretix.eu/guides/widget/), on the organizer's own site, it is
the same: the payment page shows in the widget's frame (or in a new tab, where the widget opens
the checkout so), and its copy buttons work there too.

## Requirements

- pretix **2026.7** or later, on Python **3.11** to **3.14**: the feature releases pretix
  publishes security fixes for. The plugin's tests run against pretix 2026.7.4, 2026.8.2 and
  2026.9.1, and its end-to-end test against their Docker images.
- pretix's periodic tasks (`runperiodic`, from cron) running, and preferably its task workers
  (Celery), as in any production installation of pretix (see
  [Background checks](#background-checks)).
- A merchant account of sattle.me, and an **integration** created in its app.
- **Outbound connections** from the pretix server (never from the customer's browser), direct:
  the SDK does not use the environment's proxy settings.
  - The merchant's **Nostr relays**, over WebSocket (TLS): those the integration key names, or
    the merchant's app.
  - The merchant's **Lightning address** (breez.tips for a sattle.me account) and the URLs its
    provider gives (its callback, its verify URLs), over HTTPS.
  - The **rate sources**, one after the other until one answers: CoinGecko, mempool.space,
    Coinbase, Kraken, OpenNode.
  - For an invoice with a deposit address, the **explorer**: **mempool.space**, or the
    merchant's own **Bitcoin node** when the merchant's app names one (over HTTPS, or HTTP for a
    node on the merchant's local network), with mempool.space as its fallback unless the
    merchant chose none; and **api.blockcypher.com** for a transaction no check saw in time.

  Every request, and the handshake of each WebSocket, says `User-Agent: sattleme-Checkout/1`.
  See the SDK's [What goes where](https://github.com/sattleme/checkout-python#what-goes-where).

## Installation

The plugin is distributed on [pretix's marketplace](https://marketplace.pretix.eu/), its package
on PyPI (`pretix-sattleme`). Into the Python environment of your pretix installation:

```shell
pip install pretix-sattleme
python -m pretix migrate
python -m pretix rebuild
```

then restart pretix's web and task workers. pip installs the SDK (`sattleme-checkout`, any 1.x
release) with it. To update the plugin, the same commands, with `pip install -U pretix-sattleme`.

With pretix's Docker image, build your own image with the plugin, as pretix's documentation
shows for any plugin
([Install a plugin](https://docs.pretix.eu/self-hosting/installation/docker_smallscale/)):

```dockerfile
FROM pretix/standalone:stable
USER root
RUN pip3 install pretix-sattleme
USER pretixuser
RUN cd /pretix/src && make production
```

then run that image instead of `pretix/standalone`: it migrates the database on start. Build it
again to update the plugin, or pretix.

## Configuration

1. In the sattle.me app, create an integration for your shop and copy its secret key
   (`sattleme_ck_…`).
2. In pretix, open your event, **Settings → Plugins**, and enable **sattle.me** (with the brand's
   icon).
3. Open **Settings → Payment → Bitcoin (sattle.me)**, enable the payment method, and enter the
   key. It is checked when you save it: the SDK refuses what is not an integration key, then
   reads the integration on the merchant's relays, so that the key of an integration removed in
   the app, or of one the relays do not know, is refused. Once saved, it is never shown again.
4. **Time to pay**: how long the customer has to pay an invoice, 1 to 60 minutes (15 by default).
5. Optionally, the **name** of the method and the **description** shown at checkout, in each of
   the event's languages (Markdown, as pretix's own methods take it). Without them, the method
   is called "Bitcoin", and the checkout says how a Bitcoin payment goes.

Change the key only while no Bitcoin payment is pending: each check uses the key set at its time,
and an invoice with a deposit address made under another integration's key would be judged without
its records in the merchant's registry (its lease, its tolerance, the invoices of its rest).

pretix's usual settings of a payment method apply as well: fees, availability dates, minimum and
maximum totals, sales channels, countries, hiding the method behind a link.

**When the method is offered.** At checkout, and to a customer changing their order's payment
method, only for a payment whose price the SDK takes (its
[Prices in a currency](https://github.com/sattleme/checkout-python#prices-in-a-currency)): in a
currency of the SDK's table (`currency_digits`), for an amount (the order's total with this
method's fee, or what the order owes then) of at most the decimals the SDK prices that currency
with. In most currencies, that is every amount of pretix. In a few, the SDK prices in whole units
while pretix rounds to cents (ISK, RSD or UGX, for instance): the method is offered for amounts in
whole units only. In a currency out of the table (a fund such as CHE, for instance), never. The
method's settings page says so, when the event's currency is one of those.

**Test mode.** sattle.me has no test network: in pretix's test mode too, payments are real
bitcoin payments, to the organizer's wallet. The checkout says so.

## Payments and orders

| What the SDK reports                     | What the plugin does                                                            |
| ---------------------------------------- | ------------------------------------------------------------------------------- |
| An invoice was made                      | The pretix payment is **pending**; the customer goes to the payment page.       |
| `open`                                   | Waits. Another payment method only once a check after its end finds it expired. |
| `open` with `received` (on-chain, short) | The page shows what came and a QR code for the rest (the SDK asks for it).      |
| `unconfirmed`                            | Waits for 3 confirmations (about 30 minutes), keeping the order from expiring.  |
| `paid` (Lightning, on-chain, or both)    | Confirms the pretix payment: the order is paid.                                 |
| `paid`, the order owing another amount   | Leaves it to the organizer, noting what came and what the order owes (below).   |
| `paid` with `received` other than `sats` | Confirms it, noting both: what the merchant accepted, or an overpayment.        |
| `expired`                                | The pretix payment **fails** (no email): the customer can pay again.            |
| `expired` with `received` above 0        | Leaves it to the organizer, noting what reached the wallet (below).             |

- **Only a `paid` status confirms a payment**, from the SDK's own check of the invoice that
  payment holds, on the server: never because the customer's browser says so. A payment is
  confirmed once, through pretix's own confirmation, whichever check sees it first.
- **One payment, one reference.** Each payment gets a random reference (128 bits), stored before
  the SDK is asked for its invoice, and never used for another payment: no deleted test order,
  restored backup or other installation can share it. A try that follows a failed one (a
  timeout, a worker stopped) asks for the same reference: the SDK then hands out the invoice it
  may have made meanwhile, never a second one, or reports it paid: the page then says the payment
  arrived, and the check that follows at once, in the background, confirms it.
- **The order locked.** The invoice is made with the payment and its order locked in the
  database, as pretix locks them: two requests at once (a double click, two workers) get one
  invoice.
- **One payable invoice per order.** A Lightning invoice cannot be revoked: until an invoice of
  the order is known to have ended unpaid, the customer cannot choose another payment method, nor
  get another Bitcoin invoice (the page says until when), so that the order is never paid twice.
  Known from its final status only, `expired` with nothing received, which a check after its end
  (its grace period included) gives: its time being over is not enough, a payment of its last
  seconds or an on-chain one dated in time is found by that check, however late it comes
  (without Celery workers) or long it fails (the merchant's Lightning address not answering).
  An invoice that ended underpaid keeps the order too, until the organizer settles it
  (Underpayments, below). When the organizer cancels or changes the order meanwhile, or marks it
  paid, pretix asks the plugin to cancel the payment: it refuses until the invoice's final status
  is known, and pretix keeps the payment pending and says why in the order's history. The
  invoice's checks go on.
- **An order changed after its invoice.** When an invoice is found paid, the plugin compares its
  price with what the order owes then, in its currency: its total (none once canceled), less what
  its other payments paid (a gift card, for one), net of what was refunded. When they differ (the
  order was changed, canceled or paid otherwise after the invoice was made), the order is never
  completed automatically. pretix has no state for an order the organizer must look at: the
  payment stays pending, the order's history notes the amount received and what the order owes
  now, and the organizer settles the difference with the customer, then confirms the payment by
  hand (or cancels it). Meanwhile the customer's pages say so, and offer no other way to pay.
- **Deadlines.** An invoice never outlives the moment pretix would refuse its payment or free the
  order's products: the order's last date of payments and its expiry (but for an order past its
  expiry when late payments are accepted: that is a late payment, as pretix allows). The plugin
  checks that each invoice ends in time, the SDK's 120 s of grace and the clock of the merchant's
  Lightning address included, and 5 minutes before: its checks after its end come before the
  deadline. A payment made in time is confirmed however long its confirmations took, and while
  an on-chain payment is being confirmed, its order is kept from expiring. An order pretix
  expired all the same before a check saw that payment (its periodic tasks running late, without
  Celery workers) is brought back, as pretix's backend does, when its products are still
  available; else pretix decides once the payment is confirmed (the order's history says why).
- **Underpayments.** When less than the amount comes on-chain in time, the page shows the rest,
  as a Lightning invoice the SDK asks for. The merchant may accept an on-chain payment a little
  short of the amount (its tolerance, set in the sattle.me app): the SDK then reports it `paid`.
  The merchant may also accept by hand, in the app, what an order underpaid on-chain received:
  the SDK then reports it `unconfirmed`, then `paid`, with `received` below `sats`, and the plugin
  confirms it as with the tolerance, noting what came.
  An invoice that ends underpaid all the same did not end unpaid: what came reached the wallet,
  and paying the order again would pay it twice. Its payment stays pending, the order's history
  notes what came, the customer's page asks them to contact the organizer, and the organizer
  settles the difference: confirms the payment by hand, or cancels it for the customer to pay
  again (and refunds what came).
- **Late payments.** A payment counts when it comes by the invoice's end, its grace period
  included. An on-chain payment broadcast after that reaches the merchant's wallet (the address
  is the wallet's) but pays no order: the SDK reports the invoice `expired` without it, the page
  says it expired unpaid, and nothing of it is recorded in pretix. The customer contacts the
  organizer, who finds it in the wallet (or on mempool.space, the address being in the payment's
  details) and settles it by hand.
- **Refunds** are not automatic: a Lightning payment can only be refunded to an invoice of the
  customer's, an on-chain one to an address of theirs, which pretix never asks. Refund the
  customer from the wallet, then record the refund in pretix as a manual refund.
- The organizer sees in the payment's details the invoice's reference, amount and exchange rate,
  the address, the transactions (linked to mempool.space), the proof of payment (preimage), the
  last check and its error in plain words; the order's history logs the invoices made, the
  payments being confirmed, the underpayments, the payments left to the organizer, those of
  another amount than the invoice's, and why an invoice could not be made. pretix's
  REST API gives the payment's reference, amount in sats, payment hash, address, expiry, last
  status, and whether that status is final.

## Background checks

The SDK has no webhooks: payments are checked, by the plugin, on the pretix server.

- While the customer's payment page is open: the page asks every few seconds, and the server
  checks the invoice at most every 6 seconds.
- In the background, each invoice by a chain of Celery tasks: every minute while it can be paid,
  once 10 seconds before the end of its grace period (an on-chain payment a check lists by then
  came in time, without asking BlockCypher) and once 10 seconds after, then every five minutes
  until its final status is known (an on-chain payment being confirmed). After failed checks,
  once the invoice has ended: one minute, then twice as long each time, up to every 30 minutes.
- pretix's periodic tasks restart the checks of an invoice whose chain stopped (a lost task), up
  to 200 per run. When pretix runs without Celery workers, they run the checks themselves, as
  often as cron runs them: those that are due, the longest due first, 20 at most and for about a
  minute per run. Payments are then confirmed later.

An invoice whose final status is still unknown after 90 days (an on-chain payment made in time,
still unconfirmed on a congested network) is no longer checked; it is logged on its order, whose
payment stays pending, the order keeping it: check it in the sattle.me app and settle it in pretix
by hand (confirm the payment, or cancel it).

If pretix confirms a payment but fails to mark its order paid (a plugin reacting to the payment
fails, the database goes away), the next check marks it paid, as pretix's backend would.

## Security

- **The integration key** is stored in pretix's settings store, in its database, like the keys of
  pretix's own payment providers: pretix does not encrypt it there. Once saved, it never goes back
  to the browser (the form shows `*****`, and a key refused is not sent back either), and the
  plugin hides it from the entry pretix writes in its log when the settings are saved. It never
  goes in a log, an exception or a page of the plugin; the SDK never puts it, or a key derived
  from it, in a message or a representation.
- **One key, one shop.** Create an integration for your pretix shop, and use its key nowhere else.
  If it leaks, remove the integration in the sattle.me app and create a new one: the key can
  make invoices that pay you, never spend your funds. Once it is removed, the old key makes no
  invoice, and the checks of the payments still pending find a Lightning invoice's payment, but
  fail for an invoice with a deposit address ("the integration was removed in the sattle.me
  app", in the payment's details) unless its Lightning invoice was paid: settle those in pretix
  by hand, from what the sattle.me app shows (the SDK's
  [The key is a secret](https://github.com/sattleme/checkout-python#the-key-is-a-secret)).
- **Payment links** carry a secret of the order tagged for this plugin (as pretix's own payment
  plugins do), compared in constant time: an order's code, or a link of another plugin, never
  opens them. Like the order's own link, a payment link opens the order: it is the customer's.
  The pages are `noindex` and never cached, and use no inline script (pretix's Content Security
  Policy). Nothing the browser sends decides an amount, a currency, a reference or a status.
- **Outbound requests.** The SDK connects to hosts the merchant chooses (its relays, its Lightning
  address, its Bitcoin node). It refuses IP addresses and `localhost`, follows no redirects and
  checks TLS, but for the merchant's Bitcoin node, which its app may name by an IPv4 address,
  and in plain HTTP for a host of a local network (never `localhost`): the SDK then sends it GET
  requests of mempool.space's API, with nothing but deposit addresses. A name may also lead to an
  address of your own network. So if your pretix server sits in a network with services of its
  own, or holds the keys of several merchants (events of different organizers, each with its
  key), restrict what it can reach to public addresses: a merchant's node on your network is then
  unreachable, and its fallback applies (see the SDK's
  [Outbound requests](https://github.com/sattleme/checkout-python#what-goes-where) and
  [The merchant's Bitcoin node](https://github.com/sattleme/checkout-python#the-merchants-bitcoin-node)).
- Report a vulnerability privately: see [SECURITY.md](SECURITY.md).

## Privacy

- **What the plugin stores**, in pretix's database: each payment's invoice as the SDK made it (its
  reference, amounts in sats and in the event's currency, exchange rate, Lightning invoice and,
  from 1,000 sats, a deposit address of the merchant's wallet) and what its checks found (its
  state, what came, the confirmations, the transactions, the proof of payment). No name and no
  email address: the customer's are pretix's own.
- **What the pretix server sends**, and to whom (the SDK's
  [What goes where](https://github.com/sattleme/checkout-python#what-goes-where)):
  - the merchant's **Nostr relays**, encrypted for the merchant (NIP-44): each invoice, with its
    price and its note (the event's name and the order's code), which the merchant's app shows;
  - the merchant's **Lightning address** (breez.tips for a sattle.me account): asked for each
    invoice, its amount and, when the address accepts comments, its note, then for its payment;
  - the **rate sources**, one at a time until one answers: asked for the price of bitcoin;
  - the **explorer**, for an invoice with an address: **mempool.space**, or the merchant's own
    **Bitcoin node** when the merchant's app names one (and mempool.space when the node does not
    answer, unless the merchant chose no fallback): the address and its transactions;
    **api.blockcypher.com**: a transaction, only for one no check saw in time.

  Each of them sees the pretix server's IP address, never the customer's: the customer's browser
  talks to the shop only (the payment page loads nothing from elsewhere).
- **Removing personal data.** pretix's data shredder « Payment information » (once the event is
  over and its shop offline) also removes what leads to the customer's coins on the blockchain:
  the deposit address and the transactions, from the payments' information, from pretix's log
  entries of them and from their invoices. Their references, amounts, exchange rates, dates and
  states stay, for the organizer's accounting; so do their Lightning invoices, which name the
  merchant's node, never the customer's. An invoice still being checked then (an on-chain
  payment being confirmed) loses them once final. What the merchant's relays received stays
  theirs: the merchant's app keeps its sales.

## Known limits

- **No proxy.** The SDK's connections do not use the environment's proxy settings: the pretix
  server needs direct outbound connections.
- **Currencies.** The method is offered only for a price the SDK takes
  ([Configuration](#configuration)). Its rate sources must price the event's currency too: a
  currency none of them prices, or an amount out of the merchant's limits (the bounds in sats of
  its Lightning address), is refused when the order is placed, with a message, and the customer
  chooses another method. So is the rest of an order that a gift card paid in part, when the
  SDK prices its currency in whole units and that rest is not a whole amount: pretix asks a
  payment method whether it is offered for the order's total, not for that rest.
- **Confirmations.** An on-chain payment is paid at 3 confirmations on the explorer (mempool.space,
  or the merchant's Bitcoin node), about 30 minutes: the order stays pending until then.
- **The merchant's Bitcoin node.** A node the merchant's app names on the merchant's own network
  (`http://192.168.1.10:3006`, `http://umbrel.local`) is reachable only by a pretix server on that
  network. Any other server fails every read of it, and its fallback applies: mempool.space, or,
  when the merchant chose none, no deposit address until the node answers (invoices in Lightning
  only; "We could not create your Bitcoin invoice right now" for an amount that can only be paid
  on-chain), and the checks of an invoice with an address fail meanwhile, its payment staying
  pending (never failed for want of an answer).
- **Time.** Making an invoice runs in the web request of the order, bounded at 20 seconds, after
  5 at most waiting for another request on the same payment (gunicorn stops a worker after 30
  seconds by default); it never checks a payment too. Checks run in Celery, but for one a
  customer without JavaScript asks for, and all of them on an installation without Celery
  workers: those run in the request, or in pretix's periodic tasks, bounded at 20 seconds each
  (and at about a minute in all per run of the periodic tasks). An order less than 500 seconds
  before its deadline can no longer be paid with Bitcoin: the shortest invoice (a minute), its
  grace period, the time to make it and 5 minutes for its last checks.
- **An underpayment accepted in the app, read late.** When the merchant accepts in the app what an
  order underpaid on-chain received, and a relay that holds that acceptance does not answer the
  check made after the invoice's end, that check reads the invoice as ended underpaid, which the
  plugin takes as final: the order's payment stays pending with what came noted, as for any
  invoice that ends underpaid, and the plugin does not check it again. The merchant then settles
  the order by hand, as the specification says (SPEC.md §3).
- **Deposit addresses.** Each invoice of 1,000 sats or more may lease an address of the
  integration's lot for its time: many orders left unpaid at once can empty it, the next ones are
  then payable in Lightning only until the merchant opens the app (see the SDK).

## Troubleshooting

The payment's details (in the order, in pretix's backend) and the order's history say what went
wrong, in plain words; pretix's log (the loggers `pretix_sattleme.*`) says it too, without the key.

- **The key is refused when saving the settings.**
  - "This is not the secret key of a sattle.me integration": copy the whole key from the app,
    `sattleme_ck_` included.
  - "This integration was removed in the sattle.me app": create a new integration, and enter its
    key.
  - "This integration could not be found": the merchant's relays do not hold it (yet): open the
    sattle.me app, check the key, and try again.
  - "This integration needs a newer version of the sattle.me plugin": update the plugin.
  - "This key could not be verified": the pretix server could not reach the merchant's relays or
    its Lightning address. Check its outbound connections ([Requirements](#requirements)): direct,
    without a proxy.
- **"Bitcoin payments are not available right now"** at checkout: the order's history says why
  ("No Bitcoin invoice could be created (…)"): an integration key no longer valid or removed, the
  integration not found on its relays, the merchant's Lightning address answering wrongly, or a
  clock that is off (the pretix server's: keep it synchronized).
- **"We could not create your Bitcoin invoice right now"**: the relays, the Lightning address or
  the rate sources did not answer in time, or the amount can only be paid on-chain and no deposit
  address of the integration was free (the merchant opens the app to give it more) or the
  merchant's Bitcoin node, without a fallback, did not answer. The customer tries again; it goes
  on: check the server's outbound connections, the node's included
  ([The merchant's Bitcoin node](#known-limits)).
- **"This order cannot be paid with Bitcoin"**: the SDK refused the amount or the currency
  ([Known limits](#known-limits)).
- **A payment stays pending.** The payment's details show its last check and its next one. No
  check at all: pretix's periodic tasks (cron) and its Celery workers must run. An on-chain
  payment waits for 3 confirmations, about 30 minutes, longer with a low fee. Checks that fail
  because "a blockchain explorer did not answer", again and again, for an invoice with an
  address: the merchant's app may name a Bitcoin node without a fallback that this server cannot
  reach ([The merchant's Bitcoin node](#known-limits)). "its checks
  stopped: no final status after 90 days": check the payment in the sattle.me app, and settle it
  in pretix by hand.
- **"The Bitcoin invoice ended underpaid"** in the order's history: less than the amount came,
  and reached the wallet; the payment stays pending. Settle the difference with the customer,
  then confirm the payment by hand, or cancel it for them to pay again.
- **A customer paid after the invoice ended**: the page says it expired unpaid, as the SDK reports
  it (see [Late payments](#payments-and-orders)): the bitcoin reached the wallet, and pays no
  order. Settle it with the customer by hand.
- **"… while the order owes … now"** in the order's history, the payment pending: the order
  changed after its invoice was made, then the invoice was paid. Settle the difference with the
  customer, then confirm the payment by hand in the order (or cancel it).
- **The payment page has no style, or the plugin no icon**: its static files were not collected
  (`python -m pretix rebuild`, or `make production` in a Docker image, after installing it).

## Development

The tools, pretix and the SDK come from `uv.lock`, with [uv](https://docs.astral.sh/uv/) 0.13.8
(`.github/ci-requirements.txt`):

```shell
uv sync --locked --group test --group pretix-2026-9   # or pretix-2026-7, pretix-2026-8
uv run --locked --no-sync python scripts/translations.py compile
uv run --locked --no-sync python -m pytest tests -m "not postgresql"   # SQLite
```

- **The tests** never reach the network: the plugin makes every client of the SDK in one place,
  `pretix_sattleme.sdk.new_client`, where the tests replace the client with a fake once the real
  SDK has checked the key and the options. The tests of PostgreSQL's row locks (marked
  `postgresql`) need PostgreSQL: they fail on another database, never skip. With a pretix
  configuration file whose `[database]` is a PostgreSQL one,
  `PRETIX_CONFIG_FILE=/path/to/pretix.cfg GITHUB_WORKFLOW=local uv run --locked --no-sync python
  -m pytest tests` runs them all, on pretix's migrations. `python scripts/postgres.py start <dir>`
  starts such a server and writes its configuration file, without Docker nor root (Linux x86_64,
  as a user other than root; `stop <dir>` stops it), as the CI does.
- **The checks**, as the CI runs them: `uv sync --locked --no-install-project --group test`, then
  `black --check .`, `isort --check-only .`, `flake8 .` (pretix's coding standard, with
  pep8-naming), `mypy` (strict), `check-manifest --no-build-isolation`, `python -m build
  --no-isolation --outdir dist .` and `python scripts/check_dist.py dist`, each with
  `uv run --locked --no-sync`.
- **The end-to-end test**, with Docker: `e2e/run.sh [--celery] [image]` builds the wheels,
  installs the plugin with pip into pretix's own image (`pretix/standalone`, pinned by its digest:
  2026.9.1's by default), runs it on PostgreSQL, enables the plugin and enters a key in pretix's
  backend, places orders in the shop, checks the payment page, and pays: the payment is checked by
  the page's link for browsers without JavaScript; with `--celery` (pretix's Celery workers, and
  Redis), also by the page's status endpoint through the workers, and by the invoice's background
  checks alone for a customer who left the page. The SDK is replaced there by a fake (`e2e/fake`,
  a test-only plugin installed beside it: the plugin has no switch for it).
- **The CI** (`.github/workflows/ci.yml`) runs the checks, and the tests on each supported pretix
  and Python version (one job on PostgreSQL, the others on SQLite), and the end-to-end test on
  each supported version's image, with and without Celery, on GitHub-hosted runners.
- **The translations** (German, informal German, French, Italian): `make localegen` updates the
  catalogs from the code and the templates (GNU gettext's tools, through Django), and
  `make localecompile` compiles them as the build does (`scripts/translations.py`, with Babel:
  the build needs no GNU gettext). `python scripts/translations.py compare` checks that each
  compiled catalog is GNU msgfmt's, byte for byte.
- **The pinned SDK.** `sattleme-checkout` is a dependency of the package (`>=1.0,<2`):
  installations get it from PyPI. The development and the CI run the copy pinned in `sdk/` (the
  SDK's own files at a commit of [checkout-python](https://github.com/sattleme/checkout-python),
  unchanged, with `sdk/PIN.json`: the commit, the version, each file's SHA-256), through
  `[tool.uv.sources]`; it is never distributed. Beside them, the SDK's vectors of the prices it
  takes (`sdk/spec/vectors/fiat.json`), which the tests run through the plugin's check of a
  price and the SDK's. To take a new version, from a clone of
  checkout-python: `python3 scripts/pin_sdk.py <clone> [<commit>]` (the commit a clean clone is on
  by default), which copies the files of that commit, writes `sdk/PIN.json` and updates `uv.lock`
  (with uv: Python, git and uv are all it needs). It takes only a commit that the clone's `origin`
  has on one of its branches (as last fetched), and says which clone and origin it read. Then
  `uv sync --locked --reinstall-package sattleme-checkout` (with the groups you use): an existing
  environment otherwise keeps the SDK uv built before when `sdk/pyproject.toml` did not change,
  which `tests/test_sdk_pin.py` catches; then the checks and the tests. A new major version
  changes the dependency's range in `pyproject.toml` first.
- **Dependencies** are locked exactly, with their hashes, never less than 7 days old
  (`exclude-newer`); Dependabot proposes the updates, after the same wait. Dependabot does not
  see the images of the end-to-end test (pretix's and Redis's, pinned by their digests in
  `e2e/run.sh` and the CI) nor PostgreSQL's binaries (pinned by their SHA-256 in
  `scripts/postgres.py`), nor pretix itself, which it is told to ignore (it reads the three
  groups of `pyproject.toml` as one dependency, and would move them all to the newest release).
  pretix is taken by hand: a bugfix release in its group of `pyproject.toml`, then `uv lock`; a
  new feature release as a new group (the oldest one dropped, with `compatibility` in
  `apps.py`), the CI's matrices, its image's digest.

## Releases

Releases are the maintainers' only: a maintainer pushes a tag `vX.Y.Z` (the version of
`pyproject.toml`) on a commit of `main`;
[`release.yml`](https://github.com/sattleme/checkout-pretix/blob/main/.github/workflows/release.yml)
runs the CI again, checks the tag, builds the package from the lockfile, and publishes it to PyPI
by trusted publishing, in the `pypi` environment, once a maintainer approves. The SDK goes first:
PyPI must have the version of `sattleme-checkout` this plugin needs.

**Distribution** is on pretix's marketplace only, which links each plugin to its PyPI project:
no GitHub release, no other index. Before the first release, PyPI holds a pending trusted
publisher for `pretix-sattleme` naming this repository, `release.yml` and the environment `pypi`
(and one for `sattleme-checkout`, in checkout-python), so that no token is stored anywhere. The
listing, asked for by email while the marketplace's registrations are closed
(marketplace@pretix.eu), links to this repository, public by then, and to the PyPI project; it
names the vendor, a support email and a support URL (this repository's issues), and the security
contact of [SECURITY.md](SECURITY.md). Its overview is this README's introduction, its
instructions are [Installation](#installation) and [Configuration](#configuration), and say that
refunds are made by hand ([Payments and orders](#payments-and-orders)). Whether a new release on
PyPI shows there on its own, the marketplace does not say: check the listing after each one.

## License

MIT, see [LICENSE](LICENSE). The sattle.me name and logo
(`pretix_sattleme/static/pretix_sattleme/sattleme-icon.svg`, the brand's icon, shown in pretix's
lists of plugins) are not under that license.
