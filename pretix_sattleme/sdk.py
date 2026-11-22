"""The sattle.me SDK, as this plugin calls it: every client is made by `new_client` (the seam
the tests replace with a fake of the SDK).

The integration key never stays in a frame of this plugin, a log or an exception: it is read
from the event's settings straight into the client, whose representation hides it.
"""

from pretix.base.models import Event
from sattleme_checkout import Client, SattlemeError

# The settings key of the integration key (the provider's `key` setting).
KEY_SETTING = "payment_sattleme_key"

# Per relay request and per HTTP request, connection and body included, in milliseconds.
RELAY_TIMEOUT_MS = 5_000
HTTP_TIMEOUT_MS = 8_000

# Whole calls, in seconds. An invoice is made during a web request, which gunicorn stops after
# 30 s by default; a key is verified when the settings are saved; checks run in the background.
CREATE_TIMEOUT = 20
VERIFY_TIMEOUT = 15
CHECK_TIMEOUT = 60


def new_client(key: str) -> Client:
    """A client of `key`, with this plugin's timeouts. `InvalidKeyError` when it is not an
    integration key; then no frame of this call holds its text (error trackers record the
    local variables of each frame)."""
    try:
        return Client(
            key, relay_timeout_ms=RELAY_TIMEOUT_MS, http_timeout_ms=HTTP_TIMEOUT_MS
        )
    finally:
        del key


def client(event: Event) -> Client:
    """A client of the integration key set for `event`.

    A client keeps nothing between calls but its options: one per call costs little, and a key
    changed in the settings is used at once. `InvalidKeyError` when the key is not set or is
    not an integration key.
    """
    return new_client(event.settings.get(KEY_SETTING) or "")


def error_code(error: BaseException) -> str:
    """A short, stable name of an error of a call, for logs and the stored invoice."""
    if isinstance(error, SattlemeError):
        return error.code
    if isinstance(error, TimeoutError):
        return "timeout"
    return "error"


def describe(error: BaseException) -> str:
    """An error of a call, for logs. The SDK's messages never hold a key."""
    if isinstance(error, SattlemeError):
        return f"{error.code}: {error.message}"
    if isinstance(error, TimeoutError):
        return "timeout: the call took too long"
    return type(error).__name__
