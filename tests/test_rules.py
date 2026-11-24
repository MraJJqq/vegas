import importlib.util
from pathlib import Path

MODULE = Path(__file__).parents[1] / "src" / "vegas.py"
spec = importlib.util.spec_from_file_location("vegas", MODULE)
vegas = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vegas)

def test_ema_seed_and_length():
    result = vegas.ema([1.0] * 20, 12)
    assert len(result) == 20
    assert result[10] is None
    assert result[11] == 1.0
    assert result[-1] == 1.0

def test_channel_touch_uses_wick_intersection():
    candle = (0, 0, 105, 95, 100, 1, 1)
    assert vegas.channel_touched(candle, 102, 98)
    assert not vegas.channel_touched(candle, 110, 108)

def test_catalog_count_and_unique_contracts():
    import json
    items = json.loads((Path(__file__).parents[1] / "config/instruments.json").read_text())
    assert len(items) == 22
    assert len({x["contract"] for x in items}) == 22
