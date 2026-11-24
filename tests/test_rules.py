import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
MODULE = ROOT / "src" / "vegas.py"
spec = importlib.util.spec_from_file_location("vegas", MODULE)
vegas = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vegas)

class VegasRuleTests(unittest.TestCase):
    def test_ema_seed_and_length(self):
        result = vegas.ema([1.0] * 20, 12)
        self.assertEqual(len(result), 20)
        self.assertIsNone(result[10])
        self.assertEqual(result[11], 1.0)
        self.assertEqual(result[-1], 1.0)

    def test_channel_touch_uses_wick_intersection(self):
        candle = (0, 0, 105, 95, 100, 1, 1)
        self.assertTrue(vegas.channel_touched(candle, 102, 98))
        self.assertFalse(vegas.channel_touched(candle, 110, 108))

    def test_catalog_count_and_unique_contracts(self):
        items = json.loads((ROOT / "config/instruments.json").read_text())
        self.assertEqual(len(items), 22)
        self.assertEqual(len({x["contract"] for x in items}), 22)

if __name__ == "__main__":
    unittest.main()
