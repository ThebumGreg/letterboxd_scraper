"""What every delivery method looks like. A new one (email, SMS, an iMessage gateway) is a module with a Provider subclass and a register() call."""
from dataclasses import dataclass, field

# What a send or a check came to. The dispatcher decides what each means (retry, wait for the quota, switch the channel off ...).
OK = "ok"
RETRY = "retry"                 # a temporary problem: try again later
QUOTA = "quota"                 # the provider's allowance is used up; nothing will go until it resets
BAD_CHANNEL = "bad_channel"     # this recipient cannot be reached (an invalid key, an account that is gone)
BAD_SETUP = "bad_setup"         # the administrator's side is wrong (no application token, or a rejected one)
REJECTED = "rejected"           # this message was refused and repeating it cannot help


class ProviderError(ValueError):
    """Something a person typed that cannot be right. The message is shown to them."""


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    url: str = None
    url_title: str = None
    priority: int = 0               # -2 silent ... 0 normal ... 1 urgent ... 2 emergency
    ttl_seconds: int = None         # delete from the recipient's devices after this long, where the provider can
    timestamp: int = None           # when it happened (Unix time), for a message that was held or retried


@dataclass
class Result:
    outcome: str
    detail: str = ""                # fit to show a person; never contains a key or an exception's text
    ref: str = None                 # the provider's own id for what it accepted
    retry_after: int = None         # seconds, for QUOTA
    data: dict = field(default_factory=dict)

    @property
    def ok(self):
        return self.outcome == OK


@dataclass(frozen=True)
class ConfigField:
    """One thing a person types to set a method up. The page is built from these, so a new provider needs no template."""
    name: str
    label: str
    secret: bool = False            # masked everywhere it is shown, and never sent back to a browser
    required: bool = True
    help: str = ""


def mask(value):
    """'••••••••abcd': enough to tell two keys apart, nothing that can be used."""
    value = str(value or "")
    return ("•" * 8 + value[-4:]) if len(value) > 8 else ("•" * len(value))


class Provider:
    key = ""
    label = ""
    scope = "user"                  # "user": a person may add it. "admin": only the administrator's own target (it can reach any address).
    fields = ()

    def validate(self, config):
        """The cleaned config, or a ProviderError. No network."""
        raise NotImplementedError

    def check(self, config):
        """Asks the service whether this config works. A Result; data may hold choices such as {'devices': [...]}."""
        raise NotImplementedError

    def send(self, config, message):
        """Delivers one Message. A Result. Never raises for a service problem."""
        raise NotImplementedError

    def choices(self, result):
        """After a successful check, the options the person should pick from, by field name: {'device': ['iPhone', 'iPad']}."""
        return {}

    def summary(self, config):
        """The config as it may be shown: secrets masked."""
        shown = {}
        for f in self.fields:
            value = (config or {}).get(f.name, "")
            shown[f.name] = mask(value) if f.secret and value else value
        return shown

    def default_label(self, config):
        return self.label
