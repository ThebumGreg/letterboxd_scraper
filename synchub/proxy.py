"""Believing a reverse proxy (Nginx Proxy Manager) about how a request really arrived.

`SYNCHUB_TRUST_PROXY` is a list of addresses or networks (`10.0.20.5`, `10.0.20.0/24`), separated by commas or spaces. The headers
`X-Forwarded-Proto`, `-Host` and `-For` are believed only on a connection that comes from one of them; from anyone else they are ignored, so a worker or
a laptop reaching port 5001 directly cannot pretend to be https. Empty or unset: nothing is trusted and nothing changes."""
import ipaddress
import logging
import os
import re

from werkzeug.middleware.proxy_fix import ProxyFix

logger = logging.getLogger(__name__)


def parse(text):
    """The networks in a SYNCHUB_TRUST_PROXY value. Raises ValueError naming what is wrong."""
    networks = []
    for part in re.split(r"[,\s]+", str(text or "").strip()):
        if not part:
            continue
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            raise ValueError(f"SYNCHUB_TRUST_PROXY: '{part}' is not an IP address or network.")
    return networks


class TrustedProxy:
    """WSGI wrapper: applies ProxyFix only to requests whose connection comes from a trusted address."""

    def __init__(self, app, networks):
        self.app, self.networks = app, networks
        self.fixed = ProxyFix(app, x_for=1, x_proto=1, x_host=1, x_prefix=0)

    def trusted(self, address):
        try:
            ip = ipaddress.ip_address(str(address).split("%")[0])
        except ValueError:
            return False
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        return any(ip in network for network in self.networks)

    def __call__(self, environ, start_response):
        if self.trusted(environ.get("REMOTE_ADDR")):
            return self.fixed(environ, start_response)
        return self.app(environ, start_response)


def install(flask_app, text=None):
    """Wraps the app when SYNCHUB_TRUST_PROXY is set. A bad value stops the start with a clear message."""
    text = os.getenv("SYNCHUB_TRUST_PROXY", "") if text is None else text
    networks = parse(text)
    if networks:
        flask_app.wsgi_app = TrustedProxy(flask_app.wsgi_app, networks)
        logger.info(f"Trusting forwarded headers from {', '.join(str(n) for n in networks)}.")
    return flask_app
