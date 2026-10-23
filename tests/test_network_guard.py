import pytest
import requests


def test_real_http_requests_are_blocked_in_tests():
    with pytest.raises(AssertionError, match="Test tried to send an HTTP request"):
        requests.get("https://example.com/", timeout=3)
