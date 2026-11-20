"""App updates (MDM inventory + App Store versions) and 'rolling out now' grouping, with fake Mosyle
and App Store servers (no network needed)."""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import appupdates  # noqa: E402
from clientmon import readable_app_name, rollout_key  # noqa: E402

STORE = {"com.google.Docs": {"trackName": "Google Docs", "version": "1.2026.4", "fileSizeBytes": "250000000",
                             "currentVersionReleaseDate": "2026-11-10T08:00:00Z", "bundleId": "com.google.Docs"},
         "com.apple.Keynote": {"trackName": "Keynote", "version": "14.4", "fileSizeBytes": "800000000",
                               "bundleId": "com.apple.Keynote"}}
MOSYLE_DEVICES = [
    {"serial_number": "S1", "device_name": "Cart A 01", "installed_apps": [
        {"identifier": "com.google.Docs", "name": "Docs", "version": "1.2026.2"},
        {"identifier": "com.apple.Keynote", "name": "Keynote", "short_version": "14.4"}]},
    {"serial_number": "S2", "device_name": "Cart A 02", "installed_apps": [
        {"identifier": "com.google.Docs", "name": "Docs", "version": "1.2026.4"}]},
]


class Fake(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def _json(self, obj, headers=None):
        b = json.dumps(obj).encode()
        self.send_response(200)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        Fake.calls.append(self.path)
        ids = (q.get("bundleId") or [""])[0].split(",")
        self._json({"resultCount": 0, "results": [STORE[i] for i in ids if i in STORE]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Fake.calls.append(self.path)
        if self.path.endswith("/login"):
            return self._json({}, {"Authorization": "Bearer tok"}) if body.get("password") == "pw" else self._json({})
        assert self.headers["Authorization"] == "Bearer tok"
        page = body["options"]["page"]
        devs = MOSYLE_DEVICES if (page == 1 and body["options"]["os"] == "ios") else []
        self._json({"status": "OK", "response": {"devices": devs, "rows": len(MOSYLE_DEVICES)}})


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{cls.srv.server_port}"
        appupdates.LOOKUP = base + "/lookup"
        appupdates.SEARCH = base + "/search"
        cls.base = base

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.au = appupdates.AppUpdates(os.path.join(self.dir.name, "apps.db"), None, enabled=False)
        appupdates._pause = lambda s: None     # no throttling in tests

    def tearDown(self):
        self.au.db.close()
        self.dir.cleanup()

    def test_versions(self):
        self.assertTrue(appupdates.older("1.9", "1.10"))
        self.assertFalse(appupdates.older("14.4", "14.4.0"))
        self.assertFalse(appupdates.older("2.0", "1.9"))
        self.assertIsNone(appupdates.older("", "1.0"))

    def test_csv_detects_columns(self):
        text = "Mosyle export\nSerial Number,Device Name,App Name,Bundle ID,Version\nS1,Cart A 01,Docs,com.google.Docs,1.2026.2\nS2,Cart A 02,Docs,com.google.Docs,1.2026.4\n"
        rows, info = appupdates.parse_csv(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["bundle"], "com.google.Docs")
        self.assertEqual(rows[0]["name"], "Docs")
        self.assertEqual(rows[0]["device"], "S1")
        self.assertTrue(info["perDevice"])
        with self.assertRaises(ValueError):
            appupdates.parse_csv("a,b\n1,2\n")

    def test_csv_summary_counts(self):
        rows, info = appupdates.parse_csv("App;Identifier;Version;Installs\nDocs;com.google.Docs;1.2026.2;40\n")
        self.assertEqual(rows[0]["n"], 40)
        self.assertFalse(info["perDevice"])

    def test_report_and_changes(self):
        csv1 = "Serial,App,Bundle ID,Version\nS1,Docs,com.google.Docs,1.2026.2\nS2,Docs,com.google.Docs,1.2026.2\nS1,Keynote,com.apple.Keynote,14.4\n"
        self.au.import_csv(csv1)
        self.au.refresh_store()
        r = self.au.report()
        docs = next(a for a in r["apps"] if a["bundle"] == "com.google.Docs")
        self.assertEqual((docs["pending"], docs["current"], docs["storeVersion"], docs["name"]), (2, 0, "1.2026.4", "Google Docs"))
        self.assertEqual(docs["pendingBytes"], 2 * 250_000_000)
        self.assertEqual(r["totals"]["withUpdates"], 1)
        self.assertEqual(r["apps"][0]["bundle"], "com.google.Docs")     # biggest pending download first
        # S1 updates Docs: the next import records the change
        res = self.au.import_csv(csv1.replace("S1,Docs,com.google.Docs,1.2026.2", "S1,Docs,com.google.Docs,1.2026.4"))
        self.assertEqual(res["changes"], 1)
        docs = next(a for a in self.au.report()["apps"] if a["bundle"] == "com.google.Docs")
        self.assertEqual((docs["pending"], docs["current"], docs.get("updated24h")), (1, 1, 1))
        d = self.au.devices(docs["key"])
        self.assertEqual(d["devices"][0]["device"], "S2")                # needing the update listed first
        self.assertTrue(d["devices"][0]["needsUpdate"])

    def test_newest_source_only(self):
        self.au.import_csv("Serial,App,Bundle ID,Version\nS1,Docs,com.google.Docs,1.2026.2\n")
        self.au.save_inventory("mosyle", [{"device": "S1", "bundle": "com.google.Docs", "name": "Docs", "version": "1.2026.2", "n": 1}])
        docs = next(a for a in self.au.report()["apps"] if a["bundle"] == "com.google.Docs")
        self.assertEqual(docs["devices"], 1)                              # not counted twice
        self.assertFalse(self.au.report()["configured"]["autoSync"])

    def test_mosyle_api(self):
        cfg = {"accessToken": "a", "email": "e", "password": "pw", "baseUrl": self.base + "/v2", "os": ["ios", "macos"]}
        rows, info = appupdates.Mosyle(cfg).inventory()
        self.assertEqual(info["devices"], 2)
        self.assertEqual(info["appsField"], "installed_apps")
        self.assertEqual({(r["device"], r["bundle"], r["version"]) for r in rows},
                         {("S1", "com.google.Docs", "1.2026.2"), ("S1", "com.apple.Keynote", "14.4"), ("S2", "com.google.Docs", "1.2026.4")})
        with self.assertRaises(appupdates.MosyleError):
            appupdates.Mosyle({**cfg, "password": "wrong"}).login()

    def test_rollout_grouping(self):
        a = "http://10.0.0.1:49164/itunes-assets/Purple221/v4/aa/bb/cc/1234-uuid/pre-thinned123.thinned.signed.dpkg.ipa?source=iosapps.itunes.apple.com"
        b = "http://10.0.0.1:49164/itunes-assets/Purple221/v4/aa/bb/cc/1234-uuid/pre-thinned456.thinned.signed.dpkg.ipa"
        self.assertEqual(rollout_key(a)[0], rollout_key(b)[0])
        self.assertIsNone(readable_app_name("pre-thinned123.thinned.signed.dpkg.ipa"))
        self.assertIsNone(readable_app_name("a1b2c3d4e5f6a7b8.signed.dpkg.ipa"))
        self.assertEqual(readable_app_name("Keynote-14.4.signed.dpkg.ipa"), "Keynote 14.4")


if __name__ == "__main__":
    unittest.main()
