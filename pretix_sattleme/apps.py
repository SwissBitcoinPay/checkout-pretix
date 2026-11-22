from typing import Any

from django.utils.translation import gettext_lazy

from . import __version__

try:
    from pretix.base.plugins import PLUGIN_LEVEL_EVENT, PluginConfig
except ImportError:
    raise RuntimeError("Please use pretix 2026.7 or above to run this plugin!")


class PluginApp(PluginConfig):
    default = True
    name = "pretix_sattleme"
    verbose_name = "sattle.me"
    default_auto_field = "django.db.models.BigAutoField"

    class PretixPluginMeta:
        name = "sattle.me"
        author = "sattle.me"
        description = gettext_lazy(
            "Accept Bitcoin payments, in Lightning or on-chain, paid straight to your "
            "sattle.me wallet."
        )
        visible = True
        # Shown in pretix's lists of plugins, 32 px high above the plugin's name: the brand's
        # icon, the logo needing 80 px of width and its clear space (see the README's License).
        picture = "pretix_sattleme/sattleme-icon.svg"
        version = __version__
        category = "PAYMENT"
        level = PLUGIN_LEVEL_EVENT
        compatibility = "pretix>=2026.7.0"
        settings_links = [
            (
                (gettext_lazy("Payment"), gettext_lazy("Bitcoin (sattle.me)")),
                "control:event.settings.payment.provider",
                {"provider": "sattleme"},
            ),
        ]
        navigation_links: list[Any] = []

    def ready(self) -> None:
        from . import signals  # NOQA
