#!/usr/bin/env python3
"""Vegas 隧道扫描：扫描并输出 signals.json；通知由 Bark 仓库负责。

规则：
- 中锋 = 4H EMA144/169；红峰 = 4H EMA576/676。
- 4H K线首次触碰隧道 -> 进入观察期。
- 观察期内再次触碰不重置观察时间。
- 首次触碰后进入观察期；之后即使4H价格离开对应隧道，也继续观察，不结束、不重置。
- 触碰之后（不含首次触碰时刻），任一根1H收盘价高于或低于1H EMA12 -> 信号，本轮结束。
- 信号触发后，必须等4H价格离开对应隧道，才允许下一轮。
- 同一根1H同时来自中锋/红峰 -> 只算一个信号。
"""
import json
import os
import sys
import time
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone

BASE = "https://api.bitget.com"
PRODUCT = "USDT-FUTURES"
SYMBOLS_FILE = "symbols.txt"
OUT_FILE = "signals.json"
DATA_DIR = "data"
HOUR = 3600 * 1000
H4 = 4 * HOUR
STEP = {"1H": HOUR, "4H": H4}
RATE = 0.08
CST = timezone(timedelta(hours=8))


def _get(path, params):
    import requests
    last = None
    for _ in range(3):
        try:
            response = requests.get(f"{BASE}{path}", params=params, timeout=20)
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != "00000":
                raise RuntimeError(payload.get("msg") or payload.get("code"))
            rows = sorted(payload["data"], key=lambda row: int(row[0]))
            time.sleep(RATE)
            return [
                {"t": int(row[0]), "o": float(row[1]), "h": float(row[2]),
                 "l": float(row[3]), "c": float(row[4])}
                for row in rows
            ]
        except Exception as exc:
            last = exc
            time.sleep(1)
    raise RuntimeError(f"Bitget request failed: {path} {params}: {last}") from last


def fetch_recent(symbol, gran):
    return _get("/api/v2/mix/market/candles", {
        "symbol": symbol, "productType": PRODUCT, "granularity": gran, "limit": 1000
    })


def fetch_before(symbol, gran, end_ms):
    return _get("/api/v2/mix/market/history-candles", {
        "symbol": symbol, "productType": PRODUCT, "granularity": gran,
        "endTime": end_ms, "limit": 200
    })


def cache_path(symbol, gran):
    return os.path.join(DATA_DIR, f"{symbol}_{gran}.csv")


def load_cache(symbol, gran):
    path = cache_path(symbol, gran)
    bars = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            next(handle, None)
            for line in handle:
                t, o, h, low, close = line.strip().split(",")
                bars[int(t)] = {
                    "t": int(t), "o": float(o), "h": float(h),
                    "l": float(low), "c": float(close)
                }
    return bars


def save_cache(symbol, gran, bars):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(cache_path(symbol, gran), "w", encoding="utf-8") as handle:
        handle.write("t,o,h,l,c\n")
        for bar in bars:
            handle.write(
                f"{bar['t']},{bar['o']},{bar['h']},{bar['l']},{bar['c']}\n"
            )


def sync(symbol, gran, now_ms, recent_fn=fetch_recent, before_fn=fetch_before):
    step = STEP[gran]
    bars = load_cache(symbol, gran)
    last_cached = max(bars) if bars else None
    recent = recent_fn(symbol, gran)
    if not recent:
        raise RuntimeError(
            f"{gran}: exchange returned no recent candles; refusing to scan stale cache"
        )

    for bar in recent:
        bars[bar["t"]] = bar

    oldest = recent[0]["t"]
    if last_cached is None or oldest > last_cached + step:
        end = oldest
        while True:
            batch = before_fn(symbol, gran, end)
            if not batch:
                break
            for bar in batch:
                bars.setdefault(bar["t"], bar)
            new_oldest = batch[0]["t"]
            if new_oldest >= end:
                break
            end = new_oldest
            if last_cached is not None and new_oldest <= last_cached:
                break

    # 巡检缓存内部缺口；只向交易所补取真实K线，不插值、不制造K线。
    ordered_times = sorted(bars)
    gaps = [
        (left, right) for left, right in zip(ordered_times, ordered_times[1:])
        if right - left > step
    ]
    for left, right in gaps:
        cursor = right
        attempts = 0
        while cursor - left > step and attempts < 100:
            attempts += 1
            batch = before_fn(symbol, gran, cursor)
            batch = [bar for bar in batch if left < bar["t"] < right]
            if not batch:
                break
            for bar in batch:
                bars.setdefault(bar["t"], bar)
            next_cursor = min(bar["t"] for bar in batch)
            if next_cursor >= cursor:
                break
            cursor = next_cursor

    result = [bars[t] for t in sorted(bars)]
    unresolved = [
        (left, right) for left, right in zip(
            [bar["t"] for bar in result], [bar["t"] for bar in result][1:]
        ) if right - left > step
    ]
    if unresolved:
        left, right = unresolved[0]
        raise RuntimeError(
            f"{gran}: unresolved historical gap {left}->{right} "
            f"({(right-left)//step - 1} candles missing)"
        )

    # 仅缓存已收盘K线，避免把未完成K线写入历史缓存。
    save_cache(symbol, gran, [bar for bar in result if bar["t"] + step <= now_ms])
    return result


def ema(values, period):
    multiplier = 2 / (period + 1)
    output, current = [], None
    for value in values:
        current = value if current is None else value * multiplier + current * (1 - multiplier)
        output.append(current)
    return output


def zones_of(closes, first_period, second_period):
    first = ema(closes, first_period)
    second = ema(closes, second_period)
    return [(min(a, b), max(a, b)) for a, b in zip(first, second)]


def scan_symbol(symbol, now_ms, get=sync):
    raw4 = get(symbol, "4H", now_ms)
    b4 = [bar for bar in raw4 if bar["t"] + H4 <= now_ms]
    b1 = [bar for bar in get(symbol, "1H", now_ms) if bar["t"] + HOUR <= now_ms]

    # EMA676必须具备完整的676根已收盘4H历史；不足时按扫描失败处理，
    # 不能把未充分预热的EMA结果伪装成有效扫描。
    if len(b4) < 676:
        raise RuntimeError(f"4H history insufficient for EMA676: {len(b4)}/676")
    if len(b1) < 60:
        raise RuntimeError(f"1H history insufficient for EMA12 scan: {len(b1)}/60")
    if now_ms - (b1[-1]["t"] + HOUR) > HOUR + 15 * 60 * 1000:
        raise RuntimeError("1H latest closed candle is stale")
    if now_ms - (b4[-1]["t"] + H4) > H4 + 15 * 60 * 1000:
        raise RuntimeError("4H latest closed candle is stale")

    closes4 = [bar["c"] for bar in b4]
    zone_list = [
        zones_of(closes4, 144, 169),
        zones_of(closes4, 576, 676)
    ]
    filter_line = ema([bar["c"] for bar in b1], 12)
    for bar, value in zip(b1, filter_line):
        bar["f"] = value

    times4 = [bar["t"] for bar in b4]
    groups = {index: [] for index in range(len(b4))}
    for bar in b1:
        index = bisect_right(times4, bar["t"]) - 1
        if index >= 0:
            groups[index].append(bar)

    first = max(bisect_left(times4, b1[0]["t"]), 1)
    found = set()
    for zone in zone_list:
        state, side, touch_time = "IDLE", 0, None
        for index in range(first, len(b4)):
            candle4 = b4[index]
            lower, upper = zone[index]
            touched = candle4["l"] <= upper and candle4["h"] >= lower
            hourly = groups[index]

            # 观察期一旦开启，离开隧道也继续观察；只有信号触发后的 DONE
            # 阶段，才在4H价格离开隧道后重置，允许下一轮触碰。
            if state == "DONE" and not touched:
                state, touch_time = "IDLE", None

            if touched and state == "IDLE":
                state = "WATCH"
                hit = next(
                    (bar for bar in hourly
                     if bar["l"] <= upper and bar["h"] >= lower),
                    None
                )
                touch_time = hit["t"] if hit else (
                    hourly[-1]["t"] if hourly else candle4["t"] - 1
                )

            if state == "WATCH":
                for candle1 in hourly:
                    if candle1["t"] <= touch_time:
                        continue
                    # 不限制方向：收盘价在EMA12上方或下方均可触发。
                    if candle1["c"] != candle1["f"]:
                        found.add(candle1["t"])
                        state = "DONE"
                        break

    return sorted(found)


def main():
    now_ms = int(time.time() * 1000)
    with open(SYMBOLS_FILE, encoding="utf-8") as handle:
        symbols = [
            line.strip() for line in handle
            if line.strip() and not line.lstrip().startswith("#")
        ]
    if not symbols:
        raise RuntimeError("symbols.txt is empty")

    today = datetime.fromtimestamp(now_ms / 1000, CST).date()
    midnight = datetime.combine(today, datetime.min.time(), tzinfo=CST)
    cutoff_ms = int(midnight.timestamp() * 1000)

    signals, errors = [], []
    for symbol in symbols:
        try:
            for timestamp in scan_symbol(symbol, now_ms):
                close_ms = timestamp + HOUR
                if close_ms >= cutoff_ms:
                    signals.append({
                        "id": f"{symbol}-{timestamp}",
                        "symbol": symbol,
                        "time": datetime.fromtimestamp(
                            close_ms / 1000, CST
                        ).strftime("%m-%d %H:%M"),
                        "ts": close_ms
                    })
        except Exception as exc:
            errors.append(f"{symbol}: {type(exc).__name__}: {exc}")

    signals.sort(key=lambda item: (item["ts"], item["symbol"]))
    result = {
        "generated_at": datetime.fromtimestamp(
            now_ms / 1000, CST
        ).strftime("%Y-%m-%d %H:%M:%S (UTC+8)"),
        "timezone": "Asia/Shanghai",
        "signal_date": today.isoformat(),
        "scanned": len(symbols),
        "succeeded": len(symbols) - len(errors),
        "signals": signals,
        "errors": errors
    }
    with open(OUT_FILE, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)

    print(
        f"扫描 {len(symbols)} 个品种，成功 {len(symbols) - len(errors)} 个，"
        f"信号 {len(signals)} 个，失败 {len(errors)} 个"
    )
    # 任意品种失败即整次失败，阻止提交不完整结果和触发 Bark。
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
