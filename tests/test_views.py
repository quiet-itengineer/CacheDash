"""Saved views: named layouts on the caching Mac, checked before they're stored."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import views  # noqa: E402

LAY = {"order": ["cl_live", "k_served"], "enabled": ["cl_live"], "sizes": {"cl_live": "l"}, "stack": True, "hours": 24, "bucket": "auto",
       "servers": [{"url": "http://evil"}], "theme": "dark"}


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.v = views.Views(os.path.join(self.d.name, "views.json"))

    def tearDown(self):
        self.d.cleanup()

    def test_save_keeps_only_layout(self):
        d = self.v.save("Rollout day", LAY, make_default=True)
        saved = d["views"][0]
        self.assertEqual(d["default"], "Rollout day")
        self.assertNotIn("servers", saved["layout"])            # never stores addresses or other settings
        self.assertNotIn("theme", saved["layout"])
        self.assertEqual(json.load(open(self.v.path))["views"][0]["layout"]["enabled"], ["cl_live"])

    def test_replace_rename_default_delete(self):
        self.v.save("A", LAY)
        self.v.save("a", {**LAY, "enabled": ["k_served"]})        # same name, different case: replaced
        self.assertEqual(len(self.v.list()["views"]), 1)
        self.v.set_default("A")
        self.v.rename("A", "Morning")
        self.assertEqual(self.v.list()["default"], "Morning")
        self.v.save("B", LAY)
        with self.assertRaises(ValueError):
            self.v.rename("B", "morning")
        self.v.delete("Morning")
        self.assertIsNone(self.v.list()["default"])

    def test_rejects_bad_input(self):
        for bad in ({"enabled": []}, {"enabled": ["<script>"]}, {"enabled": ["cl_live"], "sizes": {"cl_live": "xl"}},
                    {"enabled": ["cl_live"], "hours": 99999}, "nope"):
            with self.assertRaises(ValueError):
                self.v.save("x", bad)
        with self.assertRaises(ValueError):
            self.v.save("  ", LAY)
        with self.assertRaises(ValueError):
            self.v.set_default("missing")


if __name__ == "__main__":
    unittest.main()
