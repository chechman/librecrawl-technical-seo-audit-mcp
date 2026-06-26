"""Unit tests for ssrf_guard. Run: python3 -m unittest tests.test_ssrf_guard"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ssrf_guard
from ssrf_guard import BlockedURLError, validate_url, safe_get


def _addrinfo(*ips):
    """Fake socket.getaddrinfo return value for the given IP strings."""
    return [(None, None, None, "", (ip, 0)) for ip in ips]


class ValidateUrlScheme(unittest.TestCase):
    def test_rejects_non_http_schemes(self):
        for url in ("file:///etc/passwd", "gopher://x/", "ftp://h/", "data:text/html,x"):
            with self.assertRaises(BlockedURLError):
                validate_url(url)

    def test_rejects_missing_host(self):
        with self.assertRaises(BlockedURLError):
            validate_url("http:///nohost")


class ValidateUrlLiteralIPs(unittest.TestCase):
    def test_rejects_literal_private_and_local(self):
        for url in (
            "http://127.0.0.1/",
            "http://10.0.0.5:8080/",
            "http://192.168.1.1/",
            "http://169.254.169.254/latest/meta-data/",   # cloud metadata
            "http://[::1]/",
            "http://[::ffff:127.0.0.1]/",                  # IPv4-mapped loopback
            "http://0.0.0.0/",
        ):
            with self.assertRaises(BlockedURLError, msg=url):
                validate_url(url)

    def test_allows_literal_public_ip(self):
        validate_url("http://8.8.8.8/")          # no DNS needed for a literal IP
        validate_url("https://1.1.1.1/path")


class ValidateUrlHostnameResolution(unittest.TestCase):
    def test_blocks_hostname_resolving_to_internal(self):
        with mock.patch.object(ssrf_guard.socket, "getaddrinfo",
                               return_value=_addrinfo("169.254.169.254")):
            with self.assertRaises(BlockedURLError):
                validate_url("http://evil.example.com/")

    def test_blocks_if_any_resolved_ip_is_internal(self):
        # Public + internal mix must still be rejected (DNS rebinding defense).
        with mock.patch.object(ssrf_guard.socket, "getaddrinfo",
                               return_value=_addrinfo("93.184.216.34", "127.0.0.1")):
            with self.assertRaises(BlockedURLError):
                validate_url("http://mixed.example.com/")

    def test_allows_hostname_resolving_to_public(self):
        with mock.patch.object(ssrf_guard.socket, "getaddrinfo",
                               return_value=_addrinfo("93.184.216.34")):
            validate_url("https://example.com/page")

    def test_dns_failure_is_blocked(self):
        import socket as _s
        with mock.patch.object(ssrf_guard.socket, "getaddrinfo",
                               side_effect=_s.gaierror("nope")):
            with self.assertRaises(BlockedURLError):
                validate_url("http://does-not-resolve.invalid/")


class _FakeURL:
    def __init__(self, value):
        self.value = value
    def join(self, location):
        # Tests use absolute redirect targets, so join just adopts them.
        return _FakeURL(location)
    def __str__(self):
        return self.value


class _FakeResp:
    def __init__(self, status_code, location=None, url="http://start/"):
        self.status_code = status_code
        self.headers = {"location": location} if location else {}
        self.url = _FakeURL(url)


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requested = []
    def get(self, url, **kw):
        self.requested.append(url)
        return self._responses.pop(0)


class SafeGet(unittest.TestCase):
    def test_blocks_redirect_to_internal(self):
        client = _FakeClient([_FakeResp(302, location="http://169.254.169.254/")])
        with mock.patch.object(ssrf_guard.socket, "getaddrinfo",
                               return_value=_addrinfo("93.184.216.34")):
            with self.assertRaises(BlockedURLError):
                safe_get(client, "http://public.example.com/")
        # First hop fetched; redirect target rejected before second fetch.
        self.assertEqual(client.requested, ["http://public.example.com/"])

    def test_follows_redirect_to_public(self):
        client = _FakeClient([
            _FakeResp(301, location="http://public2.example.com/"),
            _FakeResp(200, url="http://public2.example.com/"),
        ])
        with mock.patch.object(ssrf_guard.socket, "getaddrinfo",
                               return_value=_addrinfo("93.184.216.34")):
            resp = safe_get(client, "http://public.example.com/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(client.requested), 2)

    def test_too_many_redirects(self):
        client = _FakeClient([_FakeResp(302, location="http://public.example.com/")
                              for _ in range(10)])
        with mock.patch.object(ssrf_guard.socket, "getaddrinfo",
                               return_value=_addrinfo("93.184.216.34")):
            with self.assertRaises(BlockedURLError):
                safe_get(client, "http://public.example.com/", max_redirects=3)


class AsyncHookIntegration(unittest.TestCase):
    """Drive the async event-hook through a real httpx.AsyncClient + MockTransport."""

    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def _getaddrinfo(self, host, *a, **k):
        # internal.* resolves to loopback; everything else to a public IP.
        ip = "127.0.0.1" if str(host).startswith("internal") else "93.184.216.34"
        return _addrinfo(ip)

    def test_blocks_initial_internal_and_allows_public(self):
        import httpx

        def handler(request):
            return httpx.Response(200, text="ok")

        async def go():
            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(
                transport=transport, event_hooks=ssrf_guard.async_guard_hooks()
            ) as client:
                resp = await client.get("http://public.example/")   # allowed
                self.assertEqual(resp.status_code, 200)
                with self.assertRaises(BlockedURLError):
                    await client.get("http://internal.svc/")         # blocked

        with mock.patch.object(ssrf_guard.socket, "getaddrinfo", self._getaddrinfo):
            self._run(go())

    def test_blocks_redirect_hop_to_internal(self):
        import httpx

        def handler(request):
            if request.url.host.startswith("internal"):
                return httpx.Response(200, text="secret")
            return httpx.Response(302, headers={"location": "http://internal.meta/"})

        async def go():
            transport = httpx.MockTransport(handler)
            async with httpx.AsyncClient(
                transport=transport, event_hooks=ssrf_guard.async_guard_hooks()
            ) as client:
                with self.assertRaises(BlockedURLError):
                    await client.get("http://public.example/", follow_redirects=True)

        with mock.patch.object(ssrf_guard.socket, "getaddrinfo", self._getaddrinfo):
            self._run(go())


if __name__ == "__main__":
    unittest.main()
