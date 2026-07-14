from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from dateutil import parser as date_parser
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
CACHE_SECONDS = 30
EVENT_PAGE_SIZE = 100
MAX_EVENT_PAGES = 20

app = FastAPI(
    title="Polymarket Weather Scanner",
    version="0.1.0",
    description="Public scanner for active Polymarket highest-temperature markets.",
)

BASE_DIR = Path(__file__).resolve().parent.parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@dataclass
class CacheEntry:
    created_at: float
    value: Any


_cache: dict[str, CacheEntry] = {}
_cache_lock = asyncio.Lock()


def _jsonish(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback
    return fallback


def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _extract_event_city_date(title: str, slug: str = "") -> tuple[str | None, str | None]:
    text = _norm(title)
    patterns = [
        r"highest temperature in (?P<city>.+?) on (?P<date>[A-Za-z]+ \d{1,2},? \d{4})",
        r"highest temperature in (?P<city>.+?) on (?P<date>[A-Za-z]+ \d{1,2})",
        r"maximum temperature in (?P<city>.+?) on (?P<date>[A-Za-z]+ \d{1,2},? \d{4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        city = match.group("city").strip(" ?")
        raw_date = match.group("date")
        try:
            default_year = date.today().year
            parsed = date_parser.parse(raw_date, default=date(default_year, 1, 1))
            if parsed.year < date.today().year - 1:
                parsed = parsed.replace(year=date.today().year)
            return city, parsed.date().isoformat()
        except (ValueError, OverflowError):
            return city, None

    slug_match = re.search(
        r"highest-temperature-in-(?P<city>.+?)-on-(?P<month>[a-z]+)-(?P<day>\d{1,2})-(?P<year>\d{4})",
        slug,
        re.IGNORECASE,
    )
    if slug_match:
        city = slug_match.group("city").replace("-", " ").title()
        raw_date = f'{slug_match.group("month")} {slug_match.group("day")} {slug_match.group("year")}'
        try:
            return city, date_parser.parse(raw_date).date().isoformat()
        except ValueError:
            return city, None
    return None, None


def _extract_temperature(question: str, group_item_title: str = "") -> tuple[str | None, float | None]:
    text = f"{_norm(question)} {_norm(group_item_title)}"
    lowered = text.lower()

    if re.search(r"\bor (?:below|lower)\b|\bor less\b|\bor under\b", lowered):
        comparator = "<="
    elif re.search(r"\bor (?:above|higher)\b|\bor more\b|\bor over\b", lowered):
        comparator = ">="
    else:
        comparator = "="

    match = re.search(r"(-?\d+(?:\.\d+)?)\s*°?\s*([cf])\b", text, re.IGNORECASE)
    if not match:
        return None, None
    value = float(match.group(1))
    unit = match.group(2).upper()
    if unit == "F":
        value = (value - 32) * 5 / 9
    return comparator, round(value, 1)


def _yes_token_and_price(market: dict[str, Any]) -> tuple[str | None, float | None]:
    outcomes = _jsonish(market.get("outcomes"), [])
    prices = _jsonish(market.get("outcomePrices"), [])
    token_ids = _jsonish(market.get("clobTokenIds"), [])

    yes_index = 0
    for index, outcome in enumerate(outcomes):
        if str(outcome).strip().lower() == "yes":
            yes_index = index
            break

    token_id = str(token_ids[yes_index]) if len(token_ids) > yes_index else None
    try:
        price = float(prices[yes_index]) if len(prices) > yes_index else None
    except (TypeError, ValueError):
        price = None
    return token_id, price


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, Any] | None = None) -> Any:
    response = await client.get(url, params=params, timeout=20)
    response.raise_for_status()
    return response.json()


async def _cached(key: str, producer) -> Any:
    now = time.time()
    existing = _cache.get(key)
    if existing and now - existing.created_at < CACHE_SECONDS:
        return existing.value

    async with _cache_lock:
        existing = _cache.get(key)
        if existing and now - existing.created_at < CACHE_SECONDS:
            return existing.value
        value = await producer()
        _cache[key] = CacheEntry(created_at=time.time(), value=value)
        return value


async def fetch_active_events() -> list[dict[str, Any]]:
    async def producer() -> list[dict[str, Any]]:
        all_events: list[dict[str, Any]] = []
        async with httpx.AsyncClient(headers={"User-Agent": "WeatherScanner/0.1"}) as client:
            for page in range(MAX_EVENT_PAGES):
                params = {
                    "active": "true",
                    "closed": "false",
                    "limit": EVENT_PAGE_SIZE,
                    "offset": page * EVENT_PAGE_SIZE,
                    "order": "volume_24hr",
                    "ascending": "false",
                }
                batch = await _get_json(client, f"{GAMMA_BASE}/events", params)
                if not isinstance(batch, list):
                    break
                all_events.extend(batch)
                if len(batch) < EVENT_PAGE_SIZE:
                    break
        return all_events

    return await _cached("active_events", producer)


async def fetch_orderbook(token_id: str | None) -> dict[str, Any]:
    if not token_id:
        return {"best_bid": None, "best_ask": None, "spread": None, "last_trade_price": None}

    async def producer() -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(headers={"User-Agent": "WeatherScanner/0.1"}) as client:
                book = await _get_json(client, f"{CLOB_BASE}/book", {"token_id": token_id})
        except (httpx.HTTPError, ValueError):
            return {"best_bid": None, "best_ask": None, "spread": None, "last_trade_price": None}

        bids = book.get("bids") or []
        asks = book.get("asks") or []

        def best(rows: list[dict[str, Any]], highest: bool) -> float | None:
            values = []
            for row in rows:
                try:
                    values.append(float(row.get("price")))
                except (TypeError, ValueError):
                    pass
            if not values:
                return None
            return max(values) if highest else min(values)

        best_bid = best(bids, True)
        best_ask = best(asks, False)
        spread = round(best_ask - best_bid, 4) if best_bid is not None and best_ask is not None else None
        try:
            last_trade = float(book.get("last_trade_price")) if book.get("last_trade_price") is not None else None
        except (TypeError, ValueError):
            last_trade = None

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "last_trade_price": last_trade,
        }

    return await _cached(f"book:{token_id}", producer)


def _looks_like_temperature_event(event: dict[str, Any]) -> bool:
    text = " ".join(
        str(event.get(key) or "")
        for key in ("title", "question", "slug", "description")
    ).lower()
    return (
        ("highest temperature" in text or "maximum temperature" in text)
        and ("temperature" in text)
    )


async def build_weather_rows(include_books: bool = True) -> list[dict[str, Any]]:
    events = await fetch_active_events()
    rows: list[dict[str, Any]] = []

    candidates: list[tuple[dict[str, Any], dict[str, Any], str, str]] = []
    for event in events:
        if not _looks_like_temperature_event(event):
            continue
        event_title = _norm(event.get("title") or event.get("question"))
        event_slug = _norm(event.get("slug"))
        city, event_date = _extract_event_city_date(event_title, event_slug)
        if not city or not event_date:
            continue

        for market in event.get("markets") or []:
            comparator, temperature_c = _extract_temperature(
                market.get("question") or "",
                market.get("groupItemTitle") or "",
            )
            if temperature_c is None:
                continue
            token_id, fallback_price = _yes_token_and_price(market)
            candidates.append((event, market, city, event_date))
            rows.append(
                {
                    "city": city,
                    "date": event_date,
                    "temperature_c": temperature_c,
                    "comparator": comparator,
                    "question": market.get("question"),
                    "event_title": event_title,
                    "event_slug": event_slug,
                    "market_slug": market.get("slug"),
                    "market_url": f"https://polymarket.com/event/{event_slug}" if event_slug else None,
                    "yes_token_id": token_id,
                    "gamma_yes_price": fallback_price,
                    "best_bid": None,
                    "best_ask": None,
                    "spread": None,
                    "last_trade_price": None,
                    "volume": _safe_float(market.get("volume")),
                    "volume_24h": _safe_float(market.get("volume24hr")),
                    "liquidity": _safe_float(market.get("liquidity")),
                }
            )

    if include_books and rows:
        semaphore = asyncio.Semaphore(20)

        async def enrich(row: dict[str, Any]) -> None:
            async with semaphore:
                row.update(await fetch_orderbook(row["yes_token_id"]))

        await asyncio.gather(*(enrich(row) for row in rows))

    rows.sort(key=lambda row: (row["date"], row["city"].lower(), row["temperature_c"]))
    return rows


def _safe_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html = (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "polymarket-weather-scanner", "cache_seconds": CACHE_SECONDS}


@app.get("/api/weather-markets")
async def weather_markets(
    city: str | None = Query(default=None),
    date_: str | None = Query(default=None, alias="date"),
    include_books: bool = Query(default=True),
) -> dict[str, Any]:
    rows = await build_weather_rows(include_books=include_books)
    if city:
        needle = city.casefold().strip()
        rows = [row for row in rows if needle in row["city"].casefold()]
    if date_:
        rows = [row for row in rows if row["date"] == date_]

    return {
        "updated_at_unix": int(time.time()),
        "count": len(rows),
        "markets": rows,
    }


@app.get("/api/weather-market")
async def weather_market(
    city: str = Query(..., min_length=2),
    date_: str = Query(..., alias="date"),
) -> dict[str, Any]:
    payload = await weather_markets(city=city, date_=date_, include_books=True)
    if payload["count"] == 0:
        raise HTTPException(status_code=404, detail="No active temperature market found for this city and date.")

    markets = payload["markets"]
    for market in markets:
        market["display_price"] = (
            ((market["best_bid"] + market["best_ask"]) / 2)
            if market["best_bid"] is not None and market["best_ask"] is not None
            else market["last_trade_price"]
            if market["last_trade_price"] is not None
            else market["gamma_yes_price"]
        )

    markets.sort(key=lambda row: (row["display_price"] or 0), reverse=True)
    return {
        "updated_at_unix": payload["updated_at_unix"],
        "city": markets[0]["city"],
        "date": date_,
        "favorite": markets[0],
        "markets": markets,
    }
