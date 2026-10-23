class ConfigError(RuntimeError):
    """A service is not configured (missing URL or API key)."""


class ApiError(RuntimeError):
    """A service answered with an unexpected HTTP status."""


def require(url, key, name):
    url = (url or "").strip().rstrip("/")
    key = (key or "").strip()
    if not url or not key:
        raise ConfigError(f"{name} URL or API key is not configured.")
    return url, key


def check(response, name, action):
    if response.status_code >= 400:
        raise ApiError(f"{name} {action} failed: HTTP {response.status_code}")
    return response
