"""Daily history and reports, built from a synthetic Metrics.db (no real cache needed)."""
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cachedash  # noqa: E402
import history  # noqa: E402

COLS = ["bytesFromCacheToClient", "bytesFromOriginToClient", "requestsFromClient"]


def make_metrics_db(path, days=10):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE ZMETRIC (Z_PK INTEGER PRIMARY KEY, ZCREATIONDATE REAL, ZPERIOD INTEGER, "
                + ", ".join(f"Z{c.upper()} INTEGER" for c in COLS) + ")")
    now = time.time()
    rows = [(now - m * 60 - cachedash.CORE_DATA_EPOCH, 60, 3000, 1000, 2) for m in range(1, days * 1440, 10)]
    con.executemany("INSERT INTO ZMETRIC (ZCREATIONDATE, ZPERIOD, " + ", ".join(f"Z{c.upper()}" for c in COLS)
                    + ") VALUES (?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return len(rows)


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.n = make_metrics_db(os.path.join(self.dir.name, "Metrics.db"))
        self.store = cachedash.MetricsStore(os.path.join(self.dir.name, "Metrics.db"))
        self.h = history.History(os.path.join(self.dir.name, "history.db"), self.store, interval=10 ** 9)

    def tearDown(self):
        self.h.db.close()
        self.dir.cleanup()

    def test_backfill_and_totals_match_source(self):
        self.h.backfill()
        self.assertGreaterEqual(len(self.h.saved_days()), 8)
        r = self.h.report((date.today() - timedelta(days=40)).isoformat(), date.today().isoformat())
        # Every source row lands in exactly one day: saved, live, or today.
        self.assertEqual(r["totals"]["requestsFromClient"], 2 * self.n)
        self.assertEqual(r["totals"]["bytesFromCacheToClient"], 3000 * self.n)
        self.assertTrue(r["coverage"]["missing"])          # days before the data started are reported as missing

    def test_saved_day_equals_live_day(self):
        d = date.today() - timedelta(days=2)
        live = self.h.compute_day(d)
        self.h.save_day(live)
        saved = self.h.load_day(d.isoformat())
        self.assertEqual(saved["totals"], live["totals"])
        self.assertEqual(saved["hourly"], live["hourly"])

    def test_bad_dates_rejected(self):
        with self.assertRaises(ValueError):
            self.h.report("2020-01-01", "2026-01-01")      # over two years
        with self.assertRaises(ValueError):
            self.h.report("not-a-date", "2026-01-01")


if __name__ == "__main__":
    unittest.main()
