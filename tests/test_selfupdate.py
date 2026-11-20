"""The dashboard's Update button: PIN checks, lockout, and the request file the updater service watches."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import selfupdate  # noqa: E402


class SelfUpdateTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.here = os.path.join(self.d.name, "cachedash")
        self.repo = os.path.join(self.d.name, "repo")
        os.makedirs(self.here)
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        self.plist = os.path.join(self.d.name, "updater.plist")
        selfupdate.UPDATER_PLIST = self.plist
        self.u = selfupdate.Updater(self.here, "1.19.0", {"path": self.repo})

    def tearDown(self):
        self.d.cleanup()

    def test_pin_hash(self):
        h = selfupdate.hash_pin("2468")
        self.assertTrue(selfupdate.check_pin("2468", h))
        self.assertFalse(selfupdate.check_pin("2469", h))
        self.assertFalse(selfupdate.check_pin("", h))
        self.assertNotIn("2468", json.dumps(h))
        with self.assertRaises(ValueError):
            selfupdate.set_pin_file(os.path.join(self.here, "p.json"), "12")

    def test_not_ready_reasons(self):
        self.assertIn("updater isn't installed", self.u.status()["why"])
        open(self.plist, "w").close()
        self.assertIn("No update PIN", self.u.status()["why"])
        self.assertEqual(self.u.request("x")[0], 409)
        self.assertIn("git clone", selfupdate.Updater(self.here, "1", {}).status()["why"])

    def test_request_and_lockout(self):
        open(self.plist, "w").close()
        selfupdate.set_pin_file(self.u.pin_path, "2468")
        self.assertEqual(oct(os.stat(self.u.pin_path).st_mode & 0o777), "0o600")
        self.assertTrue(self.u.status()["ready"])
        code, res = self.u.request("2468", "10.0.0.5")
        self.assertEqual((code, res["state"]), (200, "queued"))
        self.assertTrue(os.path.exists(self.u.request_path))
        self.assertTrue(self.u.request("2468")[1].get("already"))       # a second click doesn't queue twice
        for _ in range(5):
            self.assertEqual(self.u.request("0000")[0], 403)
        self.assertEqual(self.u.request("2468")[0], 429)                  # locked after 5 wrong PINs


if __name__ == "__main__":
    unittest.main()
