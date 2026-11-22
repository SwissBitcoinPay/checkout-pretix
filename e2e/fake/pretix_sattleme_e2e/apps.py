import logging

from pretix.base.plugins import PLUGIN_LEVEL_EVENT, PluginConfig

logger = logging.getLogger(__name__)


class PluginApp(PluginConfig):
    default = True
    name = "pretix_sattleme_e2e"
    verbose_name = "sattle.me end-to-end test (the SDK faked)"

    class PretixPluginMeta:
        name = "sattle.me end-to-end test (the SDK faked)"
        author = "sattle.me"
        description = "Test only: the sattle.me SDK faked, without any network."
        # Never offered to the events: it acts as soon as pretix starts.
        visible = False
        version = "1.0.0"
        category = "INTEGRATION"
        level = PLUGIN_LEVEL_EVENT

    def ready(self) -> None:
        from pretix_sattleme import sdk

        from .fake import FakeClient

        setattr(sdk, "Client", FakeClient)
        logger.warning("The sattle.me SDK is faked: end-to-end test only")
