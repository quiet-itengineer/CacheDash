"""Download details: where a file is stored in the cache, and which devices downloaded it."""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cachestore  # noqa: E402
import clientmon  # noqa: E402

PKG = "/content/downloads/12/34/093-55555/abcdef0123/Safari26.1SequoiaAuto.pkg"
GUID = "270493E7-E048-40E8-A9E8-C498FD34585E"


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        data = self.d.name
        con = sqlite3.connect(os.path.join(data, "AssetInfo.db"))
        con.execute("CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZGUID TEXT, ZNAMESPACE TEXT, ZURI TEXT, ZTOTALBYTES INTEGER, ZCREATIONDATE REAL)")
        con.execute("INSERT INTO ZASSET (ZGUID, ZURI, ZTOTALBYTES, ZCREATIONDATE) VALUES (?,?,?,?)",
                    (GUID, "https://swcdn.apple.com" + PKG, 1234, 800000000.0))
        con.execute("INSERT INTO ZASSET (ZGUID, ZURI, ZTOTALBYTES) VALUES (?,?,?)", ("AAAA1111-0000-0000-0000-000000000000", "https://x/other.pkg", 5))
        con.commit()
        con.close()
        os.makedirs(os.path.join(data, GUID))
        with open(os.path.join(data, GUID, "0"), "wb") as f:
            f.write(b"x" * 1234)
        self.store = cachestore.CacheStore(data_dir=data)

    def tearDown(self):
        self.d.cleanup()

    def test_where_on_disk(self):
        for url in ("http://10.0.1.15:49164" + PKG + "?source=swcdn.apple.com", "https://swcdn.apple.com" + PKG, PKG):
            r = self.store.find(url)
            self.assertEqual(len(r["matches"]), 1, url)
            m = r["matches"][0]
            self.assertEqual(m["folder"], os.path.join(self.d.name, GUID))
            self.assertTrue(m["onDisk"])
            self.assertEqual((m["diskBytes"], m["files"][0]["name"]), (1234, "0"))
            self.assertAlmostEqual(m["dates"]["CREATIONDATE"], 800000000.0 + cachestore.CORE_DATA_EPOCH)
        self.assertEqual(self.store.find("/somewhere/else/missing.pkg")["matches"], [])
        self.assertEqual(len(self.store.find("Safari26.1SequoiaAuto.pkg")["matches"]), 1)   # just the file name works too

    def test_who_downloaded_it(self):
        cm = clientmon.ClientMonitor(os.path.join(self.d.name, "clients.db"), enabled=False)
        now = time.time()
        app = "/itunes-assets/Purple221/v4/aa/bb/1111-uuid/"
        rows = [(now - 60, "10.0.0.5", "Mac15,12", "macOS 26.4", "macOS update", "x", "http://10.0.1.15:49164" + PKG + "?source=swcdn.apple.com", 1000),
                (now - 30, "10.0.0.6", "Mac14,2", "macOS 26.4", "macOS update", "x", "https://swcdn.apple.com" + PKG, 1000),
                (now - 20, "10.0.0.7", "Mac14,2", "macOS 26.4", "macOS update", "x", "https://swcdn.apple.com/content/downloads/other.pkg", 9),
                (now - 10, "10.0.0.8", "iPad13,18", "iPadOS 18.6", "App Store app", "x", "http://c" + app + "pre-thinned1.thinned.signed.dpkg.ipa", 7),
                (now - 5, "10.0.0.9", "iPad11,6", "iPadOS 18.6", "App Store app", "x", "http://c" + app + "pre-thinned2.thinned.signed.dpkg.ipa", 7)]
        cm._db.executemany("INSERT INTO downloads (t, ip, model, os, kind, item, url, bytes) VALUES (?,?,?,?,?,?,?,?)", rows)
        r = cm.downloads_of("https://swcdn.apple.com" + PKG + "?x=1")
        self.assertEqual([d["ip"] for d in r["devices"]], ["10.0.0.6", "10.0.0.5"])
        self.assertEqual((r["downloads"], r["exact"], r["bytes"]), (2, 2, 2000))
        a = cm.downloads_of("http://c" + app + "pre-thinned1.thinned.signed.dpkg.ipa")
        self.assertEqual((a["exact"], a["downloads"]), (1, 2))        # the other iPad model's version counts as a variant
        self.assertTrue(next(d for d in a["devices"] if d["ip"] == "10.0.0.9")["variant"])


if __name__ == "__main__":
    unittest.main()
