import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Tests must never reach the network: fail loudly instead of making a real HTTP call."""
    import socket
    real_connect = socket.socket.connect

    def guarded(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("localhost", "127.0.0.1", "::1") and not host.startswith("/"):
            raise AssertionError(f"Test tried to open a network connection to {host!r}")
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded)

    # Environments that send traffic through a local proxy would pass the socket check above, so guard requests too.
    import requests
    from urllib.parse import urlparse

    def guarded_send(self, request, *args, **kwargs):
        host = urlparse(request.url).hostname
        if host not in ("localhost", "127.0.0.1", "::1"):
            raise AssertionError(f"Test tried to send an HTTP request to {request.url!r}")
        return real_send(self, request, *args, **kwargs)

    real_send = requests.adapters.HTTPAdapter.send
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", guarded_send)
