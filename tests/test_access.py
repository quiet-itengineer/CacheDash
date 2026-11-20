"""Who may use the API: private networks only, and no cross-site use from outside web pages."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cachedash import Access  # noqa: E402


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.a = Access()

    def test_private_and_loopback_clients_allowed(self):
        for ip in ("127.0.0.1", "10.1.2.3", "172.16.0.9", "192.168.1.20", "::1", "::ffff:10.0.0.4", "fe80::1%en0"):
            self.assertTrue(self.a.client_ok(ip), ip)

    def test_public_clients_refused(self):
        for ip in ("8.8.8.8", "203.0.113.7", "172.32.0.1", "2001:4860:4860::8888", "not-an-ip"):
            self.assertFalse(self.a.client_ok(ip), ip)

    def test_allow_override(self):
        a = Access(["10.5.0.0/16"])
        self.assertTrue(a.client_ok("10.5.9.9"))
        self.assertFalse(a.client_ok("10.6.0.1"))
        self.assertTrue(a.client_ok("127.0.0.1"))      # the caching Mac itself is always allowed
        self.assertFalse(a.client_ok("192.168.1.5"))

    def test_origins(self):
        self.assertTrue(self.a.origin_ok(None))                          # same-origin / non-browser
        self.assertTrue(self.a.origin_ok("http://10.0.1.16:8027"))       # another caching Mac
        self.assertTrue(self.a.origin_ok("http://cache2.local:8027"))
        self.assertTrue(self.a.origin_ok("http://localhost:8027"))
        self.assertFalse(self.a.origin_ok("null"))                       # sandboxed frames, file:// pages
        self.assertFalse(self.a.origin_ok("https://203.0.113.9"))
        self.assertFalse(self.a.origin_ok("javascript:alert(1)"))

    def test_extra_origin(self):
        a = Access(origins=["https://dash.example.org/"])
        self.assertTrue(a.origin_ok("https://dash.example.org"))


if __name__ == "__main__":
    unittest.main()
