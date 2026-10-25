"""The delivery methods SyncHub knows. Importing this package registers them."""
from synchub.notifications.providers.base import ProviderError

_REGISTRY = {}


def register(provider):
    """Adds a Provider instance under its key. Returns it."""
    _REGISTRY[provider.key] = provider
    return provider


def get(key):
    try:
        return _REGISTRY[key]
    except KeyError:
        raise ProviderError("That delivery method is not available.")


def for_people():
    """The methods a person may add for themselves."""
    return [p for p in _REGISTRY.values() if p.scope == "user"]


from synchub.notifications.providers import apprise_provider, pushover  # noqa: E402,F401  (each registers itself)
