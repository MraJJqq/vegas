#!/usr/bin/env python3
"""Bitget-only Vegas scanner. No notification delivery is performed here."""
from __future__ import annotations
import argparse, datetime as dt, gzip, json, os, sqlite3, sys, time, urllib.parse, urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("VEGAS_DB", ROOT / "market-cache.sqlite"))
OUTPUT_PATH = Path(os.getenv("VEGAS_OUTPUT", ROOT / "data/signals.json"))
INSTRUMENTS = json.loads((ROOT / "config/instruments.json").read_text())
TZ = ZoneInfo("Asia/Shanghai")
BASE = "https://api.bitget.com"
INTERVAL_MS = {"1H": 3_600_000, "4H": 14_400_000}
EMA_PERIODS = {"1H": (12,), "4H": (144, 169, 576, 676)}
REQUEST_PAUSE = float(os.getenv("BITGET_REQUEST_PAUSE", "0.08"))

def now_ms() -> int:
    return int(time.time() * 1000)

def beijing_now() -> dt.datetime:
    return dt.datetime.now(TZ)

def api_get(path: str, params: dict) -> dict:
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "VegasScanner/1.0"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("code") != "00000":
                raise RuntimeError(f"Bitget API error: {payload}")
            return payload
        except Exception:
            if attempt == 5:
                raise
            time.sleep(min(2 ** attempt, 20))
    raise RuntimeError("unreachable")

def verify_contracts() -> tuple[list[dict], list[dict]]:
    payload = api_get("/api/v3/market/instruments", {"category": "USDT-FUTURES"})
    available = {str(x.get("symbol", "")).upper() for x in payload.get("data", [])}
    valid, missing = [], []
    for item in INSTRUMENTS:
        (valid if item["contract"].upper() in available else missing).append(item)
    return valid, missing

def db_connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS candles(
      contract TEXT NOT NULL, interval TEXT NOT NULL, ts INTEGER NOT NULL,
      open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
      volume REAL, turnover REAL, PRIMARY KEY(contract, interval, ts)
    );
    CREATE INDEX IF NOT EXISTS idx_candles_lookup ON candles(contract, interval, ts);
    CREATE TABLE IF NOT EXISTS observations(
      contract TEXT NOT NULL, channel TEXT NOT NULL, touch_ts INTEGER NOT NULL,
      triggered_ts INTEGER, PRIMARY KEY(contract, channel)
    );
    CREATE TABLE IF NOT EXISTS signals(
      signal_id TEXT PRIMARY KEY, beijing_date TEXT NOT NULL, symbol TEXT NOT NULL,
      contract TEXT NOT NULL, channel TEXT NOT NULL, touch_ts INTEGER NOT NULL,
      trigger_ts INTEGER NOT NULL, close REAL NOT NULL, ema12 REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(beijing_date, trigger_ts);
    CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    return con

def candle_rows(data: list) -> list[tuple]:
    rows = []
    for x in data:
        if len(x) < 7:
            continue
        rows.append((int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]), float(x[6])))
    return rows

def save_candles(con: sqlite3.Connection, contract: str, interval: str, data: list) -> int:
    rows = [(contract, interval, *r) for r in candle_rows(data)]
    if rows:
        con.executemany("INSERT OR REPLACE INTO candles(contract,interval,ts,open,high,low,close,volume,turnover) VALUES(?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
    return len(rows)

def fetch_page(contract: str, interval: str, start_ms: int, end_ms: int) -> list:
    payload = api_get("/api/v3/market/history-candles", {
        "category": "USDT-FUTURES", "symbol": contract, "interval": interval,
        "type": "market", "startTime": str(start_ms), "endTime": str(end_ms), "limit": "100"
    })
    time.sleep(REQUEST_PAUSE)
    data = payload.get("data") or []
    return sorted(data, key=lambda x: int(x[0]))

def latest_ts(con: sqlite3.Connection, contract: str, interval: str):
    row = con.execute("SELECT MAX(ts) FROM candles WHERE contract=? AND interval=?", (contract, interval)).fetchone()
    return row[0]

def backfill(con: sqlite3.Connection, contract: str, interval: str) -> int:
    """Walk backwards in 100-candle pages until Bitget returns no older candles."""
    end = now_ms()
    inserted = 0
    seen_oldest = None
    while end > 0:
        start = max(0, end - 90 * 24 * 60 * 60 * 1000)
        page = fetch_page(contract, interval, start, end)
        if not page:
            break
        inserted += save_candles(con, contract, interval, page)
        oldest = min(int(x[0]) for x in page)
        if seen_oldest == oldest or oldest >= end:
            break
        seen_oldest = oldest
        end = oldest - 1
        if oldest <= start:
            end = start - 1
    return inserted

def update_incremental(con: sqlite3.Connection, contract: str, interval: str) -> int:
    last = latest_ts(con, contract, interval)
    if last is None:
        return backfill(con, contract, interval)
    start = int(last) + INTERVAL_MS[interval]
    end = now_ms()
    inserted = 0
    while start < end:
        page_end = min(end, start + 90 * 24 * 60 * 60 * 1000)
        page = fetch_page(contract, interval, start, page_end)
        if not page:
            break
        inserted += save_candles(con, contract, interval, page)
        newest = max(int(x[0]) for x in page)
        if newest < start or newest >= page_end or len(page) < 100:
            break
        next_start = newest + INTERVAL_MS[interval]
        if next_start <= start:
            break
        start = next_start
    return inserted

def load_candles(con: sqlite3.Connection, contract: str, interval: str) -> list[tuple]:
    return con.execute(
        "SELECT ts,open,high,low,close,volume,turnover FROM candles WHERE contract=? AND interval=? ORDER BY ts",
        (contract, interval)
    ).fetchall()

def ema(values: list[float], period: int) -> list[float | None]:
    out = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    alpha = 2.0 / (period + 1)
    current = seed
    for i in range(period, len(values)):
        current = values[i] * alpha + current * (1.0 - alpha)
        out[i] = current
    return out

def channel_touched(candle: tuple, ema_a: float, ema_b: float) -> bool:
    # A 4H wick intersects the complete EMA band.
    band_low, band_high = sorted((ema_a, ema_b))
    return float(candle[3]) <= band_high and float(candle[2]) >= band_low

def scan_one(con: sqlite3.Connection, item: dict) -> int:
    contract = item["contract"]
    h1 = load_candles(con, contract, "1H")
    h4 = load_candles(con, contract, "4H")
    if len(h1) < 12 or len(h4) < 676:
        print(f"INSUFFICIENT_HISTORY {contract}: 1H={len(h1)} 4H={len(h4)}")
        return 0

    # Exclude the currently forming candle in each timeframe.
    current = now_ms()
    h1 = [x for x in h1 if int(x[0]) + INTERVAL_MS["1H"] <= current]
    h4 = [x for x in h4 if int(x[0]) + INTERVAL_MS["4H"] <= current]
    if not h1 or not h4:
        return 0

    h1_ema = ema([float(x[4]) for x in h1], 12)
    h4_close = [float(x[4]) for x in h4]
    h4_emas = {p: ema(h4_close, p) for p in (144, 169, 576, 676)}
    created = 0

    # Touch observation is persistent until the first later closed 1H candle
    # confirms a close on either side of EMA12. Leaving the channel does not cancel it.
    for i, candle in enumerate(h4):
        if any(h4_emas[p][i] is None for p in (144,169,576,676)):
            continue
        ts = int(candle[0])
        for channel, p1, p2 in (("middle",144,169),("red",576,676)):
            if not channel_touched(candle, h4_emas[p1][i], h4_emas[p2][i]):
                continue
            row = con.execute("SELECT touch_ts,triggered_ts FROM observations WHERE contract=? AND channel=?", (contract, channel)).fetchone()
            if row is None:
                con.execute("INSERT INTO observations(contract,channel,touch_ts,triggered_ts) VALUES(?,?,?,NULL)", (contract,channel,ts))
            elif row[1] is not None and ts > int(row[0]):
                con.execute("UPDATE observations SET touch_ts=?,triggered_ts=NULL WHERE contract=? AND channel=?", (ts,contract,channel))
    con.commit()

    for channel in ("middle", "red"):
        state = con.execute("SELECT touch_ts,triggered_ts FROM observations WHERE contract=? AND channel=?", (contract,channel)).fetchone()
        if not state or state[1] is not None:
            continue
        touch_ts = int(state[0])
        eligible = [(i,c) for i,c in enumerate(h1) if int(c[0]) >= touch_ts + INTERVAL_MS["4H"]]
        for i, candle in eligible:
            e = h1_ema[i]
            if e is None or int(candle[0]) + INTERVAL_MS["1H"] > current:
                continue
            close = float(candle[4])
            if close == float(e):
                continue
            trigger_ts = int(candle[0])
            signal_id = f"{contract}:{channel}:{touch_ts}:{trigger_ts}"
            date_key = beijing_now().date().isoformat()
            con.execute(
                "INSERT OR IGNORE INTO signals(signal_id,beijing_date,symbol,contract,channel,touch_ts,trigger_ts,close,ema12) VALUES(?,?,?,?,?,?,?,?,?)",
                (signal_id,date_key,item.get("signalSymbol",item["symbol"]),contract,channel,touch_ts,trigger_ts,close,float(e))
            )
            con.execute("UPDATE observations SET triggered_ts=? WHERE contract=? AND channel=?", (trigger_ts,contract,channel))
            created += 1
            break
    con.commit()
    return created

def export_signals(con: sqlite3.Connection) -> None:
    today = beijing_now().date().isoformat()
    # Signal records expire at Beijing midnight; candle cache and observations remain.
    con.execute("DELETE FROM signals WHERE beijing_date<>?", (today,))
    con.commit()
    rows = con.execute(
        "SELECT symbol,contract,channel,touch_ts,trigger_ts,close,ema12 FROM signals WHERE beijing_date=? ORDER BY trigger_ts,symbol",
        (today,)
    ).fetchall()
    output = {
        "date": today,
        "timezone": "Asia/Shanghai",
        "updatedAt": beijing_now().isoformat(timespec="seconds"),
        "signals": [
            {"symbol":r[0],"contract":r[1],"channel":r[2],"touchTime":iso_ms(r[3]),
             "triggerTime":iso_ms(r[4]),"close":r[5],"ema12":r[6]}
            for r in rows
        ]
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")

def iso_ms(value: int) -> str:
    return dt.datetime.fromtimestamp(value/1000, TZ).isoformat(timespec="seconds")

def run_warmup():
    valid, missing = verify_contracts()
    print(f"CONTRACTS valid={len(valid)} unavailable={len(missing)}")
    for x in missing:
        print(f"UNAVAILABLE {x['symbol']} {x['contract']}")
    if missing:
        raise RuntimeError("Some configured contracts are not listed by Bitget; refusing to mark warmup complete.")
    con = db_connect()
    for n, item in enumerate(valid, 1):
        for interval in ("1H", "4H"):
            count = backfill(con, item["contract"], interval)
            total = con.execute("SELECT COUNT(*) FROM candles WHERE contract=? AND interval=?", (item["contract"],interval)).fetchone()[0]
            print(f"[{n}/{len(valid)}] {item['contract']} {interval}: added={count} cached={total}")
    con.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('last_full_warmup',?)", (beijing_now().isoformat(),))
    con.commit()
    export_signals(con)

def run_scan():
    valid, missing = verify_contracts()
    print(f"CONTRACTS valid={len(valid)} unavailable={len(missing)}")
    for x in missing:
        print(f"UNAVAILABLE {x['symbol']} {x['contract']}")
    con = db_connect()
    for item in valid:
        for interval in ("1H","4H"):
            update_incremental(con, item["contract"], interval)
    total = 0
    for item in valid:
        total += scan_one(con, item)
    export_signals(con)
    print(f"SCAN_COMPLETE valid={len(valid)} unavailable={len(missing)} new_signals={total} output={OUTPUT_PATH}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("warmup","scan"))
    args = parser.parse_args()
    if args.command == "warmup":
        run_warmup()
    else:
        run_scan()

if __name__ == "__main__":
    main()
