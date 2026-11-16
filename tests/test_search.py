"""Tracking one device: exact address matching in the log and the device list."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logs  # noqa: E402
from clientmon import ClientMonitor  # noqa: E402


class SearchTests(unittest.TestCase):
    def test_log_ip_is_exact(self):
        p = logs.ip_pattern("10.0.0.5")
        self.assertTrue(p.search('request from 10.0.0.5:51000 for http://…'))
        self.assertTrue(p.search("10.0.0.5"))
        self.assertFalse(p.search("from 10.0.0.50:1"))      # longer address
        self.assertFalse(p.search("from 110.0.0.5:1"))      # longer at the front
        self.assertFalse(p.search("1.10.0.0.5"))
        self.assertIsNone(logs.ip_pattern("timed out"))    # plain text search is unchanged

    def test_device_find(self):
        devs = [{"ip": "10.0.0.5", "name": "Cart A iPad 05", "model": "iPad13,18", "os": "iPadOS 18.6", "bytes": 5},
                {"ip": "10.0.0.50", "name": None, "model": "Mac15,12", "os": "macOS 26.4", "bytes": 9}]
        self.assertEqual([d["ip"] for d in ClientMonitor._find(devs, "10.0.0.5")], ["10.0.0.5"])
        self.assertEqual([d["ip"] for d in ClientMonitor._find(devs, "10.0.0")], ["10.0.0.50", "10.0.0.5"])
        self.assertEqual([d["ip"] for d in ClientMonitor._find(devs, "IPAD")], ["10.0.0.5"])
        self.assertEqual(ClientMonitor._find(devs, "nothing"), [])


if __name__ == "__main__":
    unittest.main()
