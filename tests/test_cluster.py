"""Connecting caching Macs: address clean-up, peers from AssetCacheManagerUtil, and joining."""
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cluster  # noqa: E402


class FakeDash(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        b = json.dumps({"name": "Cache 2", "version": "1.18.0"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


class ClusterTests(unittest.TestCase):
    def test_normalize(self):
        n = cluster.normalize
        self.assertEqual(n("10.0.1.16"), "http://10.0.1.16:8027")
        self.assertEqual(n("10.0.1.16:9000"), "http://10.0.1.16:9000")
        self.assertEqual(n("http://Cache2.local:8027/"), "http://cache2.local:8027")
        self.assertEqual(n("cache2", 8030), "http://cache2:8030")
        for bad in ("", "10.0.1.16:99999", "javascript:alert(1)", "http://a b", "http://x/path"):
            self.assertIsNone(n(bad), bad)

    def test_peer_addresses(self):
        st = {"Peers": [{"address": "10.0.1.16:49219"}, {"Address": "10.0.1.17"}], "Parents": [{"address": "10.0.1.16"}]}
        self.assertEqual(cluster.peer_addresses(st), ["10.0.1.16", "10.0.1.17"])
        self.assertEqual(cluster.peer_addresses({}), [])

    def test_join_checks_and_saves(self):
        ip = lan_ip()
        if not ip or ip.startswith("127."):
            self.skipTest("no LAN address")
        srv = ThreadingHTTPServer(("0.0.0.0", 0), FakeDash)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_port
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "servers.json")
            c = cluster.Cluster(path, 8027, "Cache 1", enabled=False)
            c.mine = set()                                   # pretend the LAN address is another Mac
            self.assertFalse(c.join("127.0.0.1", port)["ok"])          # loopback is this Mac
            self.assertFalse(c.join(ip, 70000)["ok"])                   # bad port
            self.assertFalse(c.join(ip, port + 1 if port < 65535 else port - 1)["ok"])   # nothing answers there
            r = c.join(ip, port)
            self.assertTrue(r["ok"] and r["added"])
            self.assertTrue(c.join(ip, port)["ok"])                      # again: not duplicated
            saved = json.load(open(path))
            self.assertEqual(saved, [{"name": "Cache 2", "url": f"http://{ip}:{port}"}])
            self.assertEqual(cluster.Cluster(path, 8027, "x", enabled=False).list()[0]["source"], "configured")
            c.mine = {ip}
            self.assertFalse(c.join(ip, port)["ok"])                     # its own address is refused
        srv.shutdown()


if __name__ == "__main__":
    unittest.main()
