"""The 'update available' check reads the VERSION line of cachedash.py on GitHub (faked here)."""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cachedash  # noqa: E402


class Raw(BaseHTTPRequestHandler):
    body = b'#!/usr/bin/env python3\nVERSION = "99.0.0"\n'

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(Raw.body)


class UpdateTests(unittest.TestCase):
    def test_versions(self):
        self.assertGreater(cachedash.vtuple("1.17.0"), cachedash.vtuple("1.16.2"))
        self.assertGreater(cachedash.vtuple("1.10"), cachedash.vtuple("1.9.9"))

    def test_source_and_check(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "source.json")
            with open(p, "w") as f:
                json.dump({"repo": "https://github.com/example/CacheDash.git", "branch": "main", "path": "/Users/me/CacheDash-repo"}, f)
            u = cachedash.UpdateCheck(p, enabled=False)
            self.assertEqual(u.raw, "https://raw.githubusercontent.com/example/CacheDash/main/cachedash.py")
            self.assertEqual(u.state["repo"], "https://github.com/example/CacheDash")
            srv = ThreadingHTTPServer(("127.0.0.1", 0), Raw)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            u.raw = f"http://127.0.0.1:{srv.server_port}/cachedash.py"
            u.check()
            self.assertTrue(u.state["available"])
            self.assertEqual(u.state["latest"], "99.0.0")
            Raw.body = f'VERSION = "{cachedash.VERSION}"\n'.encode()
            u.check()
            self.assertFalse(u.state["available"])
            srv.shutdown()

    def test_ssh_remote_and_no_source(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "source.json")
            with open(p, "w") as f:
                json.dump({"repo": "https://github.com:example/CacheDash.git"}, f)
            self.assertIn("example/CacheDash/main", cachedash.UpdateCheck(p, enabled=False).raw)
            self.assertIsNone(cachedash.UpdateCheck(os.path.join(d, "none.json"), enabled=False).raw)


if __name__ == "__main__":
    unittest.main()
