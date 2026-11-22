"""The end-to-end test's organizer and customers, over HTTP, as browsers without JavaScript.

    python e2e/driver.py [--celery] <pretix's URL>

Against a pretix where pretix-sattleme was installed from its wheel, beside the fake of the SDK
(e2e/fake), and e2e/setup_event.py was run: the organizer enables the plugin and enters a key;
a customer orders, chooses Bitcoin, gets the payment page (its files, its translation, its status
endpoint); the fake's invoice is paid; the page's link to check the payment, without JavaScript,
has the server's own check see it, and the order is paid.

With --celery (a pretix whose Celery workers run): a second customer pays as the page's script
does, asking its status endpoint, whose checks run in the workers; a third one pays and leaves
the page at once, so that only the invoice's background checks (a chain of Celery tasks) can see
the payment, which nothing else asks for. Each step prints what it checked; the first that fails
ends the test (exit status 1).
"""

import html
import http.cookiejar
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import Message

# The specification's test vector of an integration key ("the app's vector"): valid, of no
# merchant. The fake answers in the merchant's place.
KEY = (
    "sattleme_ck_qEVejHuCZpF4XMmBSw5p7iZYxCtm20XxzT1fnJ9VB8o."
    "uPTAz1d8bbQaw2Jx22Wwrga2vI2F8allY5NrPC3Aogk.cmVsYXkuZXhhbXBsZQ"
)
# Those of e2e/setup_event.py, and of the fake's paid invoices (e2e/fake).
EMAIL = "e2e@example.org"
PASSWORD = "e2e-test-password"
EVENT = "/e2e/conf"
PREIMAGE = "e2e0" * 16
CONTROL = f"/control/event{EVENT}"
# The page pretix shows while a task of its workers runs (placing an order, filling a cart): it
# reloads itself.
WAITING = "We are processing your request"
# Seconds a payment may take to be seen: a check of the page's at most every 6 s; the first
# background check of an invoice a minute after it was made, on a busy machine.
PAYMENT_SEEN_WITHIN = 300


class CheckError(Exception):
    pass


def check(condition: object, what: str) -> None:
    if not condition:
        raise CheckError(what)
    print(f"ok: {what}")


@dataclass
class Page:
    url: str
    status: int
    headers: Message
    text: str

    def csrf(self) -> str:
        found = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', self.text)
        if found is None:
            raise CheckError(f"no CSRF token in {self.url}")
        return found.group(1)


class Browser:
    """Cookies kept, redirects followed, pretix's pages of tasks under way reloaded until the
    task ended (as their own refresh does), no proxy (pretix runs here), no JavaScript.
    """

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )

    def request(
        self,
        path: str,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Page:
        page = self._open(path, data, headers)
        deadline = time.monotonic() + 120
        while WAITING in page.text:
            if time.monotonic() > deadline:
                raise CheckError(f"a task of pretix never ended ({page.url})")
            time.sleep(1)
            page = self._open(page.url, None, headers)
        return page

    def _open(
        self, path: str, data: dict[str, str] | None, headers: dict[str, str] | None
    ) -> Page:
        url = path if path.startswith("http") else self.base + path
        body = None if data is None else urllib.parse.urlencode(data).encode()
        request = urllib.request.Request(url, body, headers or {})
        try:
            with self.opener.open(request, timeout=60) as response:
                return Page(
                    response.geturl(),
                    response.status,
                    response.headers,
                    response.read().decode(),
                )
        except urllib.error.HTTPError as e:
            return Page(
                e.geturl(), e.code, e.headers, e.read().decode(errors="replace")
            )

    def get(self, path: str, headers: dict[str, str] | None = None) -> Page:
        return self.request(path, headers=headers)

    def post(self, path: str, data: dict[str, str]) -> Page:
        return self.request(path, data)

    def submit(self, page: Page, data: dict[str, str]) -> Page:
        """The page's form sent back to its own URL, with its CSRF token."""
        return self.post(page.url, {"csrfmiddlewaretoken": page.csrf(), **data})


@dataclass
class Order:
    """An order placed with Bitcoin: its customer's browser, its code, its payment page."""

    customer: Browser
    code: str
    page: Page

    @property
    def path(self) -> str:
        return urllib.parse.urlsplit(self.page.url).path

    def status(self) -> dict[str, object]:
        """What the page's status endpoint answers (the page's script asks it)."""
        answer: dict[str, object] = json.loads(
            self.customer.get(self.path + "status/").text
        )
        return answer

    def pay(self) -> None:
        bolt11 = re.search(r'id="sattleme-lightning">(lnbc[0-9a-z]+)<', self.page.text)
        check(
            bolt11, f"the page of order {self.code} shows the Lightning invoice to copy"
        )
        assert bolt11 is not None
        paid = self.customer.post("/_e2e/sattleme/pay", {"bolt11": bolt11.group(1)})
        check(paid.status == 200, "its customer pays it (the fake of the SDK)")

    def paid(self, page: Page, how: str) -> None:
        said = re.search(r'<p class="lead">\s*(.*?)\s*(?:<br>|</p>)', page.text, re.S)
        check(
            page.url.endswith("?paid=yes")
            and "We successfully received your payment." in page.text,
            f"{how}: order {self.code} is paid (the page at {page.url}, HTTP "
            f"{page.status}, says: {said.group(1) if said else '(nothing)'})",
        )


def wait_for(base: str) -> None:
    """Until pretix serves its login page (its /healthcheck/ needs a cache, Redis or
    memcached, which the test's pretix may lack)."""
    browser = Browser(base)
    deadline = time.monotonic() + 600
    while True:
        try:
            if browser.get("/control/login").status == 200:
                break
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise CheckError(f"pretix did not answer at {base}")
        time.sleep(2)
    print(f"ok: pretix answers at {base}")


def organizer_sets_up(base: str) -> Browser:
    admin = Browser(base)
    page = admin.get("/control/login")
    page = admin.submit(page, {"email": EMAIL, "password": PASSWORD})
    check(urllib.parse.urlsplit(page.url).path == "/control/", "the organizer logs in")

    page = admin.get(f"{CONTROL}/settings/plugins")
    check("sattle.me" in page.text, "the plugin is in the event's list of plugins")
    icon = re.search(
        r'src="([^"]*/pretix_sattleme/sattleme-icon[^"]*\.svg)"', page.text
    )
    check(icon, "the list shows its icon")
    assert icon is not None
    picture = admin.get(icon.group(1))
    check(
        picture.status == 200
        and picture.headers.get_content_type() == "image/svg+xml"
        and 'aria-label="sattle.me"' in picture.text,
        f"pretix serves the icon ({icon.group(1)})",
    )
    page = admin.submit(page, {"plugin:pretix_sattleme": "enable"})
    check(
        "The plugin <strong>sattle.me</strong> is now active" in page.text,
        "the organizer enables the plugin",
    )

    settings = f"{CONTROL}/settings/payment/sattleme"
    page = admin.get(settings)
    check(page.status == 200 and "Integration key" in page.text, "its settings open")
    page = admin.submit(
        page,
        {
            "payment_sattleme__enabled": "on",
            "payment_sattleme_key": KEY,
            "payment_sattleme_invoice_minutes": "15",
            "payment_sattleme__restrict_to_sales_channels": "web",
        },
    )
    check(
        "Your changes have been saved." in page.text,
        "the organizer enters the key: checked (the fake reads the merchant), saved",
    )
    page = admin.get(settings)
    check(
        KEY not in page.text and 'value="*****"' in page.text,
        "the settings never show the key again",
    )
    return admin


def place_order(base: str) -> Order:
    """A new customer orders a ticket with Bitcoin, and gets the payment page."""
    customer = Browser(base)
    page = customer.get(f"{EVENT}/")
    item = re.search(r'name="(item_\d+)"', page.text)
    check(page.status == 200 and item, "the shop shows the product")
    assert item is not None
    page = customer.post(
        f"{EVENT}/cart/add", {"csrfmiddlewaretoken": page.csrf(), item.group(1): "1"}
    )
    check(page.status == 200, "a customer puts it in the cart")
    page = customer.get(f"{EVENT}/checkout/start")
    check(page.url.endswith("/checkout/questions/"), "the checkout asks for the e-mail")
    page = customer.submit(page, {"email": "customer@example.org"})
    check(
        page.url.endswith("/checkout/payment/") and 'value="sattleme"' in page.text,
        "the payment step offers Bitcoin",
    )
    page = customer.submit(page, {"payment": "sattleme"})
    check(page.url.endswith("/checkout/confirm/"), "the customer chooses it")
    page = customer.submit(page, {})
    paying = re.search(
        rf"{EVENT}/sattleme/([A-Z0-9]+)/[0-9a-f]{{64}}/[0-9]+/$", page.url
    )
    check(paying, f"the order is placed, and leads to the payment page ({page.url})")
    assert paying is not None
    check(
        page.status == 200
        and "<svg" in page.text
        and "Scan this QR code with your Bitcoin wallet." in page.text
        and "49,000 sats" in page.text,
        "the page shows the invoice's QR code and its amount",
    )
    return Order(customer, paying.group(1), page)


def customer_pays_without_javascript(base: str) -> str:
    """The page as served, then paid and checked by its link for browsers without JavaScript:
    the order's code."""
    order = place_order(base)
    page = order.page
    check(
        page.headers.get("X-Robots-Tag") == "noindex"
        and "no-store" in page.headers.get("Cache-Control", ""),
        "the page is never indexed nor cached",
    )
    check(
        "<script>" not in page.text and 'onclick="' not in page.text,
        "no script of the page runs inline (pretix's Content Security Policy)",
    )
    loads_only_the_shops_files(order.customer, page)
    # Another browser, in German: the link alone opens the page (the order's secret for this
    # plugin), in the customer's language (the catalogs the wheel holds, compiled).
    german = Browser(base).get(order.path, headers={"Accept-Language": "de"})
    check(
        "Scannen Sie diesen QR-Code mit Ihrem Bitcoin-Wallet." in german.text,
        "the page speaks the customer's language (German, from the wheel's translations)",
    )
    answer = order.status()
    check(
        answer.get("phase") == "open" and "redirect" not in answer,
        "the page's status endpoint says the invoice waits for its payment",
    )
    order.pay()

    # Without JavaScript: the page's link that has the server check the payment (at most every
    # few seconds), until that check confirms it and the page sends the customer to the order.
    link = re.search(r'href="([^"]*\?check=1)"', page.text)
    check(link, "the page offers to check the payment without JavaScript")
    assert link is not None
    deadline = time.monotonic() + PAYMENT_SEEN_WITHIN
    while True:
        page = order.customer.get(html.unescape(link.group(1)))
        if page.url.endswith("?paid=yes") or time.monotonic() > deadline:
            break
        time.sleep(2)
    order.paid(
        page, "the server's own check, asked by the page's link, saw the payment"
    )
    answer = order.status()
    check(
        answer.get("phase") == "paid"
        and str(answer.get("redirect", "")).endswith("?paid=yes"),
        "the page's status endpoint says so too, and leads to the order",
    )
    return order.code


def customer_pays_with_the_pages_script(base: str) -> str:
    """With Celery's workers: the page's status endpoint, as its script asks it every few
    seconds, has the workers check the payment, and later says what they found."""
    order = place_order(base)
    order.pay()
    deadline = time.monotonic() + PAYMENT_SEEN_WITHIN
    while True:
        answer = order.status()
        if answer.get("phase") == "paid" or time.monotonic() > deadline:
            break
        time.sleep(3)
    check(
        answer.get("phase") == "paid",
        "the status endpoint's checks, run by the workers, saw the payment",
    )
    order.paid(
        order.customer.get(str(answer["redirect"])), "the page leads to the order"
    )
    return order.code


def customer_pays_and_leaves(base: str) -> str:
    """With Celery's workers: the customer pays and leaves the payment page at once; only the
    invoice's background checks, a chain of tasks of the workers, can see the payment (pretix's
    periodic tasks do not run in the test's pretix: no cron). The payment page, opened again,
    shows what the checks found, and checks nothing itself.
    """
    order = place_order(base)
    order.pay()
    deadline = time.monotonic() + PAYMENT_SEEN_WITHIN
    while True:
        page = order.customer.get(order.path)
        if page.url.endswith("?paid=yes") or time.monotonic() > deadline:
            break
        time.sleep(5)
    order.paid(
        page, "the invoice's background checks saw the payment of a customer gone"
    )
    return order.code


def loads_only_the_shops_files(customer: Browser, page: Page) -> None:
    """The page's script and stylesheet are the plugin's static files, served by the shop, and
    nothing it loads comes from another host."""
    loaded = re.findall(
        r'<(?:script|link|img|iframe)\b[^>]*\s(?:src|href)="([^"]*)"', page.text
    )
    elsewhere = [
        url
        for url in map(html.unescape, loaded)
        if url.startswith("//")
        or (not url.startswith("/") and not url.startswith(customer.base + "/"))
    ]
    check(loaded and not elsewhere, "the page loads nothing from another host")
    for name, kind in (("pay.js", "javascript"), ("pay.css", "css")):
        stem, suffix = name.split(".")
        found = [
            url
            for url in loaded
            if re.fullmatch(
                rf"/static/pretix_sattleme/{stem}(\.[0-9a-f]+)?\.{suffix}", url
            )
        ]
        check(found, f"the page loads the plugin's {name}")
        served = customer.get(found[0])
        check(
            served.status == 200 and kind in served.headers.get_content_type(),
            f"the shop serves it ({found[0]})",
        )


def organizer_sees_it(admin: Browser, code: str) -> None:
    page = admin.get(f"{CONTROL}/orders/{code}/")
    check(page.status == 200, f"the organizer opens the order {code}")
    check(
        "Proof of payment" in page.text and PREIMAGE in page.text,
        "its payment shows the invoice's proof of payment",
    )


def main(base: str, celery: bool) -> int:
    try:
        wait_for(base)
        admin = organizer_sets_up(base)
        codes = [customer_pays_without_javascript(base)]
        if celery:
            codes.append(customer_pays_with_the_pages_script(base))
            codes.append(customer_pays_and_leaves(base))
        for code in codes:
            organizer_sees_it(admin, code)
    except CheckError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    print("the end-to-end test passed")
    return 0


if __name__ == "__main__":
    arguments = sys.argv[1:]
    celery = arguments[:1] == ["--celery"]
    if celery:
        arguments = arguments[1:]
    if len(arguments) != 1:
        sys.exit(__doc__)
    sys.exit(main(arguments[0], celery))
