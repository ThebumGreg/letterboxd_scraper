"""Apprise: one URL per target (email, SMS, Telegram, ntfy and many more). Because a URL can point anywhere, only the administrator's own
target uses it (Settings, Notifications); a person cannot add it for themselves."""
import logging
import re

from synchub.notifications.providers import register
from synchub.notifications.providers.base import BAD_CHANNEL, OK, RETRY, ConfigField, Provider, ProviderError, Result

logger = logging.getLogger(__name__)


def split_urls(text):
    return [u for u in re.split(r"[,\s]+", str(text or "")) if u]


class AppriseTarget(Provider):
    key = "apprise"
    label = "Apprise"
    scope = "admin"
    fields = (ConfigField("urls", "Apprise target URLs", secret=True, help="One or more, separated by spaces or commas. They contain access tokens."),)

    def validate(self, config):
        urls = split_urls((config or {}).get("urls"))
        if not urls:
            raise ProviderError("No Apprise URLs set.")
        return {"urls": " ".join(urls)}

    def _notifier(self, config):
        import apprise
        notifier = apprise.Apprise()
        understood = sum(1 for url in split_urls(config["urls"]) if notifier.add(url))
        return notifier, understood

    def check(self, config):
        try:
            config = self.validate(config)
        except ProviderError as exc:
            return Result(BAD_CHANNEL, str(exc))
        _, understood = self._notifier(config)
        if not understood:
            return Result(BAD_CHANNEL, "Apprise does not understand any of those URLs.")
        return Result(OK, data={"understood": understood})

    def send(self, config, message):
        try:
            config = self.validate(config)
        except ProviderError as exc:
            return Result(BAD_CHANNEL, str(exc))
        notifier, understood = self._notifier(config)
        if not understood:
            return Result(BAD_CHANNEL, "Apprise does not understand any of those URLs.")
        try:
            sent = bool(notifier.notify(title=message.title, body=message.body))
        except Exception as exc:                                  # the text of a failure can quote the target URL, token included
            logger.error(f"Apprise could not send: {exc.__class__.__name__}")
            return Result(RETRY, "Apprise could not send the message.")
        return Result(OK) if sent else Result(RETRY, "Apprise could not deliver the message (see the log).")


register(AppriseTarget())
