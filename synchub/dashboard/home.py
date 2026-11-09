"""The minute job that asks UniFi who is on the Wi-Fi once and gives the answer to both users of it: the morning "left for school" rules and Welcome home."""
from synchub import unifi_network
from synchub.dashboard import config, school, welcome


class _Once:
    """Wraps the UniFi client so the list is fetched at most once per round (an error is remembered too)."""

    def __init__(self, client=None):
        self.client, self.done, self.result = client, False, None

    def online_macs(self):
        if not self.done:
            try:
                self.result = (self.client or unifi_network.NetworkClient()).online_macs()
            except unifi_network.NetworkError as exc:
                self.result = exc
            self.done = True
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def poll(now=None, client=None):
    now = now or config.now()
    once = _Once(client)
    school.poll(now, once)
    welcome.poll(now, once)
