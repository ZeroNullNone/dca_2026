#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import time
import uuid
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DASHBOARD_PATH = DATA_DIR / "dashboard.json"
EXECUTIONS_PATH = DATA_DIR / "executions.json"

BINANCE_BASES = (
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
)

STAGES = [
    {"id": "light", "name": "Light value"},
    {"id": "main", "name": "Main value"},
    {"id": "deep", "name": "Deep value"},
    {"id": "extreme", "name": "Extreme value"},
    {"id": "manual", "name": "Manual / recovery"},
]
REFERENCE_LEVELS = (
    ("Level 1", 1.00),
    ("Level 2", 0.98),
    ("Level 3", 0.95),
    ("Level 4", 0.90),
)
GENESIS_DATE = datetime(2009, 1, 3, tzinfo=timezone.utc).date()


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("'\"")
        values[key.strip()] = value
    return values


def load_config() -> dict[str, str]:
    config = {}
    # ponytail: .env.example is a usable local source because this is a private tool; .env overrides it.
    config.update(parse_env_file(ROOT / ".env.example"))
    config.update(parse_env_file(ROOT / ".env"))
    config.update(os.environ)
    return config


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def day_iso(timestamp_ms: int | float) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).date().isoformat()


def day_iso_seconds(timestamp_s: int | float) -> str:
    return datetime.fromtimestamp(timestamp_s, timezone.utc).date().isoformat()


def read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def write_json(path: Path, data) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)


def dashboard_cache():
    data = read_json(DASHBOARD_PATH, {})
    if isinstance(data.get("latest"), dict):
        data["score"] = score_payload(data["latest"])
        data["ladder"] = reference_ladder(data["latest"])
    data["stages"] = STAGES
    data["buckets"] = STAGES
    return data


def http_json(url: str, params: dict | None = None, headers: dict | None = None, timeout: int = 25):
    if params:
        joiner = "&" if "?" in url else "?"
        url = f"{url}{joiner}{urlencode(params)}"
    req = Request(url, headers={"User-Agent": "btc-dca-dashboard/1.0", **(headers or {})})
    with urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def binance_json(path: str, params: dict | None = None):
    last_error = None
    for base in BINANCE_BASES:
        try:
            return http_json(f"{base}{path}", params=params)
        except Exception as exc:  # pragma: no cover - network fallback
            last_error = exc
    raise RuntimeError(f"Binance request failed: {last_error}")


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[list]:
    rows: list[list] = []
    cursor = start_ms
    seen: set[int] = set()
    while cursor < end_ms:
        batch = binance_json(
            "/api/v3/klines",
            {"symbol": symbol, "interval": interval, "startTime": cursor, "endTime": end_ms, "limit": 1000},
        )
        if not batch:
            break
        for row in batch:
            open_time = int(row[0])
            if open_time not in seen:
                seen.add(open_time)
                rows.append(row)
        next_cursor = int(batch[-1][0]) + 1
        if next_cursor <= cursor or len(batch) < 1000:
            break
        cursor = next_cursor
        time.sleep(0.08)
    return sorted(rows, key=lambda row: row[0])


def rolling_mean(values: list[float | None], window: int) -> list[float | None]:
    out: list[float | None] = []
    total = 0.0
    q: list[float] = []
    for value in values:
        if value is None or not math.isfinite(value):
            out.append(None)
            q.clear()
            total = 0.0
            continue
        q.append(value)
        total += value
        if len(q) > window:
            total -= q.pop(0)
        out.append(total / window if len(q) == window else None)
    return out


def ahr999_value(price: float | None, dma200: float | None, day: str | None) -> float | None:
    if not isinstance(price, (int, float)) or not isinstance(dma200, (int, float)) or not day:
        return None
    if price <= 0 or dma200 <= 0 or not math.isfinite(price) or not math.isfinite(dma200):
        return None
    days = (datetime.fromisoformat(day).date() - GENESIS_DATE).days
    if days <= 0:
        return None
    fitted_price = 10 ** (5.84 * math.log10(days) - 17.01)
    return (price / dma200) * (price / fitted_price) if fitted_price > 0 else None


def glassnode_metric(config: dict[str, str], path: str, since_s: int, until_s: int) -> list[dict]:
    api_key = config.get("GLASSNODE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("missing GLASSNODE_API_KEY")
    return http_json(
        f"https://api.glassnode.com{path}",
        params={"a": config.get("GLASSNODE_ASSET", "BTC"), "s": since_s, "u": until_s, "i": "24h"},
        headers={"X-Api-Key": api_key},
    )


def latest_non_null(series: list[dict], key: str):
    for point in reversed(series):
        value = point.get(key)
        if isinstance(value, (int, float)) and math.isfinite(value):
            return value
    return None


def value_map(rows: list[dict]) -> dict[str, float]:
    mapped = {}
    for row in rows:
        value = row.get("v")
        if isinstance(value, dict):
            value = next((v for v in value.values() if isinstance(v, (int, float))), None)
        if isinstance(value, (int, float)) and math.isfinite(value):
            mapped[day_iso_seconds(row["t"])] = float(value)
    return mapped


def fetch_fear_greed(limit: int) -> dict[str, float]:
    data = http_json("https://api.alternative.me/fng/", {"limit": limit, "format": "json"})
    out = {}
    for row in data.get("data", []):
        try:
            out[day_iso_seconds(int(row["timestamp"]))] = float(row["value"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def score_payload(latest: dict) -> dict:
    rules = [
        ("price_wma", "BTC vs 200WMA", latest.get("price"), latest.get("wma200"), (1.00, 0.95)),
        ("mvrv_z", "MVRV Z-Score", latest.get("mvrv_z"), None, (0.10, -0.20)),
        ("nupl", "NUPL", latest.get("nupl"), None, (0.12, 0.0)),
        ("puell", "Puell Multiple", latest.get("puell"), None, (0.60, 0.50)),
        ("ath_drawdown", "ATH drawdown", latest.get("ath_drawdown"), None, (-0.55, -0.60)),
    ]
    rendered = []
    for key, label, a, b, tiers in rules:
        known = isinstance(a, (int, float)) and (b is None or isinstance(b, (int, float)))
        points = None
        if known:
            value = a / b if key == "price_wma" else a
            one, two = tiers
            points = 2 if value <= two else 1 if value <= one else 0
        rendered.append(
            {
                "key": key,
                "label": label,
                "value": a,
                "compare": b,
                "tiers": tiers,
                "points": points,
                "pass": points is not None and points > 0,
            }
        )
    score = sum(rule["points"] or 0 for rule in rendered)
    known = sum(1 for rule in rendered if rule["points"] is not None)
    action = "Update unavailable metrics"
    detail = "Use cached price data until every confirmation metric refreshes."
    if known == 5:
        if score <= 3:
            action, detail = "No confirmation buy", "AHR999 may be cheap, but confirmation score does not unlock budget."
        elif score == 4:
            action, detail = "Divergence mode", "Small probe only. Cap and pace are both reduced."
        elif score <= 6:
            action, detail = "Normal value DCA", "Use the AHR999 zone at the standard pace."
        elif score <= 8:
            action, detail = "Accelerated DCA", "Confirmation is strong enough to raise target and pace."
        else:
            action, detail = "Capitulation DCA", "Full zone target and fastest planned weekly pace are unlocked."
    return {"score": score, "known": known, "rules": rendered, "action": action, "detail": detail}


def reference_ladder(latest: dict) -> list[dict]:
    ladder = []
    for label, multiple in REFERENCE_LEVELS:
        target = latest["wma200"] * multiple if latest.get("wma200") else None
        ladder.append(
            {
                "label": label,
                "multiple": multiple,
                "offset": multiple - 1,
                "target": target,
                "distance": target / latest["spot"] - 1 if target and latest.get("spot") else None,
            }
        )
    return ladder


def build_dashboard_data() -> dict:
    config = load_config()
    symbol = config.get("BINANCE_SYMBOL", "BTCUSDT")
    history_days = int(config.get("DASHBOARD_HISTORY_DAYS", "3650"))
    chart_days = int(config.get("DASHBOARD_CHART_DAYS", "1460"))
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - history_days * 86_400_000
    since_s = start_ms // 1000
    until_s = now_ms // 1000
    errors: list[dict] = []

    ticker = binance_json("/api/v3/ticker/24hr", {"symbol": symbol})
    daily_rows = fetch_klines(symbol, "1d", start_ms, now_ms)
    weekly_rows = fetch_klines(symbol, "1w", start_ms, now_ms)

    daily = [
        {
            "date": day_iso(int(row[0])),
            "price": float(row[4]),
            "high": float(row[2]),
            "low": float(row[3]),
        }
        for row in daily_rows
    ]
    closes = [row["price"] for row in daily]
    dma200 = rolling_mean(closes, 200)

    weekly_closes = [float(row[4]) for row in weekly_rows]
    weekly_wma = rolling_mean(weekly_closes, 200)
    weekly_points = [(day_iso(int(row[0])), weekly_wma[idx]) for idx, row in enumerate(weekly_rows)]

    glassnode_maps: dict[str, dict[str, float]] = {}
    metric_paths = {
        "mvrv_z": config.get("GLASSNODE_MVRV_Z_PATH", "/v1/metrics/market/mvrv_z_score"),
        "nupl": config.get("GLASSNODE_NUPL_PATH", "/v1/metrics/indicators/net_unrealized_profit_loss"),
        "puell": config.get("GLASSNODE_PUELL_PATH", "/v1/metrics/indicators/puell_multiple"),
    }
    for key, path in metric_paths.items():
        try:
            glassnode_maps[key] = value_map(glassnode_metric(config, path, since_s, until_s))
        except Exception as exc:
            glassnode_maps[key] = {}
            errors.append({"source": "glassnode", "metric": key, "message": str(exc)})

    try:
        fear_map = fetch_fear_greed(min(history_days, 3000))
    except Exception as exc:
        fear_map = {}
        errors.append({"source": "alternative.me", "metric": "fear", "message": str(exc)})

    series = []
    ath = None
    wma_idx = 0
    current_wma = None
    for idx, row in enumerate(daily):
        while wma_idx < len(weekly_points) and weekly_points[wma_idx][0] <= row["date"]:
            if weekly_points[wma_idx][1] is not None:
                current_wma = weekly_points[wma_idx][1]
            wma_idx += 1
        ath = row["high"] if ath is None else max(ath, row["high"])
        point = {
            **row,
            "wma200": current_wma,
            "dma200": dma200[idx],
            "ahr999": ahr999_value(row["price"], dma200[idx], row["date"]),
            "ath_drawdown": row["price"] / ath - 1 if ath else None,
            "mvrv_z": glassnode_maps["mvrv_z"].get(row["date"]),
            "nupl": glassnode_maps["nupl"].get(row["date"]),
            "puell": glassnode_maps["puell"].get(row["date"]),
            "fear": fear_map.get(row["date"]),
        }
        series.append(point)

    latest = {key: latest_non_null(series, key) for key in (
        "price", "wma200", "dma200", "ahr999", "ath_drawdown", "mvrv_z", "nupl", "puell", "fear"
    )}
    latest.update(
        {
            "date": series[-1]["date"] if series else None,
            "spot": float(ticker["lastPrice"]),
            "change_24h": float(ticker["priceChangePercent"]) / 100,
            "high_24h": float(ticker["highPrice"]),
            "low_24h": float(ticker["lowPrice"]),
            "quote_volume_24h": float(ticker["quoteVolume"]),
        }
    )
    latest["price"] = latest["spot"]
    if latest.get("dma200"):
        latest["ahr999"] = ahr999_value(latest["spot"], latest["dma200"], latest.get("date"))
    if latest.get("wma200"):
        latest["price_vs_wma"] = latest["spot"] / latest["wma200"] - 1

    ladder = reference_ladder(latest)

    data = {
        "meta": {
            "refreshed_at": iso_now(),
            "symbol": symbol,
            "asset": config.get("GLASSNODE_ASSET", "BTC"),
            "history_days": history_days,
            "chart_days": chart_days,
            "status": "partial" if errors else "ok",
            "sources": {
                "binance": "ok",
                "glassnode": "ok" if not any(e["source"] == "glassnode" for e in errors) else "unavailable",
                "fear_greed": "ok" if fear_map else "unavailable",
            },
            "errors": errors,
        },
        "latest": latest,
        "score": score_payload(latest),
        "ladder": ladder,
        "stages": STAGES,
        "buckets": STAGES,
        "series": series[-chart_days:],
    }
    write_json(DASHBOARD_PATH, data)
    return data


def executions() -> list[dict]:
    return read_json(EXECUTIONS_PATH, [])


def save_executions(rows: list[dict]) -> None:
    write_json(EXECUTIONS_PATH, rows)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def send_json(self, data, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/data":
            if not DASHBOARD_PATH.exists():
                build_dashboard_data()
            return self.send_json(dashboard_cache())
        if parsed.path == "/api/executions":
            return self.send_json(executions())
        if parsed.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/refresh":
            try:
                return self.send_json(build_dashboard_data())
            except Exception as exc:
                return self.send_json({"error": str(exc), "cached": dashboard_cache() if DASHBOARD_PATH.exists() else None}, 502)
        if parsed.path == "/api/executions":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            rows = executions()
            usdt = float(payload.get("usdt") or 0)
            price = float(payload.get("price") or 0)
            fee = float(payload.get("fee") or 0)
            btc = float(payload.get("btc") or 0) or ((usdt - fee) / price if usdt and price else 0)
            record = {
                "id": str(uuid.uuid4()),
                "date": payload.get("date") or iso_now(),
                "bucket": payload.get("bucket") or "manual",
                "usdt": usdt,
                "price": price,
                "btc": btc,
                "fee": fee,
                "note": str(payload.get("note") or "")[:180],
            }
            rows.append(record)
            save_executions(rows)
            return self.send_json(record, 201)
        return self.send_error(404)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/executions/"):
            target = parsed.path.rsplit("/", 1)[-1]
            rows = [row for row in executions() if row.get("id") != target]
            save_executions(rows)
            return self.send_json({"ok": True})
        return self.send_error(404)


def self_check() -> None:
    assert rolling_mean([1, 2, 3], 2) == [None, 1.5, 2.5]
    assert round(ahr999_value(20_000, 25_000, "2022-12-31"), 3) == 0.361
    score = score_payload(
        {"price": 60_000, "wma200": 62_000, "ahr999": 0.4, "mvrv_z": 0.2, "nupl": 0.1, "puell": 0.5, "fear": 20, "ath_drawdown": -0.5}
    )
    assert score["score"] == 4
    assert score["known"] == 5
    assert [level["multiple"] for level in reference_ladder({"wma200": 100, "spot": 125})] == [1.0, 0.98, 0.95, 0.9]
    print("self-check ok")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if args.fetch:
        build_dashboard_data()
        print(f"wrote {DASHBOARD_PATH}")
        return

    config = load_config()
    host = args.host or config.get("SERVER_HOST", "127.0.0.1")
    port = args.port if args.port is not None else int(config.get("SERVER_PORT", "8765"))
    if not DASHBOARD_PATH.exists():
        try:
            build_dashboard_data()
        except Exception as exc:
            print(f"initial refresh failed: {exc}")
    server = ThreadingHTTPServer((host, port), Handler)
    actual_host, actual_port = server.server_address
    print(f"Dashboard: http://{actual_host}:{actual_port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
