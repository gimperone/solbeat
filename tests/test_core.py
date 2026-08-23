import sys
import time
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from core.anomaly import evaluate, _zscore  # noqa: E402
from core.store import Store  # noqa: E402
from core.util import pct_change  # noqa: E402


SCHEMA = """
CREATE TABLE metrics (ts REAL NOT NULL, key TEXT NOT NULL, value REAL NOT NULL);
CREATE TABLE snapshots (ts REAL PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE sources (name TEXT PRIMARY KEY, last_ok REAL, last_status TEXT, items INTEGER DEFAULT 0);
CREATE TABLE alerts (ts REAL NOT NULL, level TEXT NOT NULL, metric TEXT NOT NULL, message TEXT NOT NULL);
CREATE TABLE news_seen (link_hash TEXT PRIMARY KEY, first_seen REAL);
CREATE TABLE news (link TEXT PRIMARY KEY, first_seen REAL, source TEXT, title TEXT,
                   published REAL, summary TEXT);
"""


class TempStore(Store):
    def __init__(self):
        # bypass file db for unit tests
        import sqlite3
        self._lock = __import__("threading").Lock()
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        self._db.executescript(SCHEMA)


class TestUtil(unittest.TestCase):
    def test_pct_change(self):
        self.assertAlmostEqual(pct_change(110, 100), 10.0)
        self.assertAlmostEqual(pct_change(50, 200), -75.0)
        self.assertIsNone(pct_change(5, 0))
        self.assertIsNone(pct_change(None, 5))


class TestZscore(unittest.TestCase):
    def test_flat_series_returns_none_on_zero_stdev(self):
        self.assertIsNone(_zscore([5.0] * 20, 5.0))

    def test_outlier_detected(self):
        vals = [10.0] * 19 + [50.0]
        z = _zscore(vals, 50.0)
        self.assertIsNotNone(z)
        self.assertGreater(z, 3)

    def test_small_window_none(self):
        self.assertIsNone(_zscore([1.0, 2.0], 9.9))


class TestAnomalyRules(unittest.TestCase):
    CFG = {
        "anomaly": {
            "window_size": 120, "z_threshold": 3.0,
            "avg_slot_ms_warn": 600, "avg_slot_ms_crit": 1000,
            "delinquent_pct_warn": 1.0, "delinquent_pct_crit": 5.0,
            "tps_drop_pct": 50, "price_move_pct": 15,
            "tvl_move_pct": 10, "stables_move_pct": 5,
        }
    }

    def test_slot_time_critical(self):
        store = TempStore()
        snap = {"metrics": {"avg_slot_ms": 1200}}
        alerts = evaluate(snap, store, self.CFG)
        self.assertTrue(any(a["level"] == "crit" and a["metric"] == "avg_slot_ms" for a in alerts))

    def test_delinquency_warning(self):
        store = TempStore()
        snap = {"metrics": {"delinquent_stake_pct": 2.5}}
        alerts = evaluate(snap, store, self.CFG)
        self.assertTrue(any(a["level"] == "warn" for a in alerts))

    def test_price_move_triggers_with_history(self):
        store = TempStore()
        now = time.time()
        with store._lock:
            store._db.executemany("INSERT INTO metrics(ts,key,value) VALUES (?,?,?)",
                                  [(now - 300, "sol_price_usd", 100.0)])
            store._db.commit()
        snap = {"metrics": {"sol_price_usd": 130.0}}  # +30%
        alerts = evaluate(snap, store, self.CFG)
        self.assertTrue(any("SOL price" in a["message"] for a in alerts))

    def test_price_move_fires_under_production_order(self):
        """Regression: production records the current value BEFORE evaluate().
        The baseline must be the previous sample, not the just-recorded one."""
        store = TempStore()
        store.record_metrics({"sol_price_usd": 100.0})   # previous tick
        store.record_metrics({"sol_price_usd": 130.0})   # current tick (+30%)
        snap = {"metrics": {"sol_price_usd": 130.0}}
        alerts = evaluate(snap, store, self.CFG)
        self.assertTrue(any("SOL price" in a["message"] for a in alerts),
                         "move rule must fire when current is already in history")

    def test_no_move_alert_when_stable(self):
        store = TempStore()
        store.record_metrics({"sol_price_usd": 100.0})
        store.record_metrics({"sol_price_usd": 101.0})   # +1% < threshold
        snap = {"metrics": {"sol_price_usd": 101.0}}
        alerts = evaluate(snap, store, self.CFG)
        self.assertFalse([a for a in alerts if "SOL price" in a["message"]])

    def test_healthy_snapshot_silent(self):
        store = TempStore()
        snap = {"metrics": {"avg_slot_ms": 400, "delinquent_stake_pct": 0.02, "tps_total": 3000}}
        alerts = evaluate(snap, store, self.CFG)
        self.assertEqual(alerts, [])


class TestReportHelpers(unittest.TestCase):
    def test_downsample_passthrough_small(self):
        from core.report import _downsample
        rows = [(1.0, 5.0), (2.0, 6.0)]
        out = _downsample(rows, 120)
        self.assertEqual(out, [[1000.0, 5.0], [2000.0, 6.0]])

    def test_downsample_reduces_large(self):
        from core.report import _downsample
        rows = [(float(i), float(i)) for i in range(1000)]
        out = _downsample(rows, 120)
        self.assertEqual(len(out), 120)
        # endpoints preserved approximately (first point exact)
        self.assertEqual(out[0], [0.0, 0.0])


class TestPriceDivergenceRule(unittest.TestCase):
    def test_divergence_warns_above_threshold(self):
        store = TempStore()
        cfg = {"anomaly": {"price_divergence_warn": 1.5}}
        snap = {"metrics": {"cg_bin_divergence_pct": 2.1}}
        alerts = evaluate(snap, store, cfg)
        self.assertTrue(any("divergence" in a["message"].lower() for a in alerts))

    def test_divergence_silent_below(self):
        store = TempStore()
        cfg = {"anomaly": {"price_divergence_warn": 1.5}}
        snap = {"metrics": {"cg_bin_divergence_pct": 0.4}}
        alerts = evaluate(snap, store, cfg)
        self.assertEqual([a for a in alerts if "divergence" in a["metric"]], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
