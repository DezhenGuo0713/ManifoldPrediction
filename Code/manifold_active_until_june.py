#!/usr/bin/env python3
"""Fetch active Manifold markets closing by the end of June.

Run directly:

    python manifold_active_until_june.py

The script writes:

    C:/GuoDezhen/ResearchProject/PredictionMarket/ManifoldPrediction/Markets/
    active_markets_until_end_june_2026_with_descriptions.csv

The output only includes unresolved markets that have not closed yet and
had at least one trade during the 24 hours before the script runs.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, time as datetime_time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


API_BASE = "https://api.manifold.markets/v0"
TIMEZONE = "America/New_York"
CUTOFF_YEAR = 2026
CUTOFF_MONTH = 6
CUTOFF_DAY = 30
OUTPUT_DIR = (
    r"C:\GuoDezhen\ResearchProject\PredictionMarket\ManifoldPrediction\Markets"
)
OUTPUT_CSV = os.path.join(
    OUTPUT_DIR,
    "active_markets_until_end_june_2026_with_descriptions.csv",
)
PAGE_LIMIT = 1000
PAGE_SLEEP_SECONDS = 0.01
DESCRIPTION_SLEEP_SECONDS = 0.0
TRADE_LOOKBACK_HOURS = 72


def fetch_json(
    path: str,
    params: dict[str, Any] | None = None,
    max_retries: int = 5,
) -> Any:
    query = urlencode(params or {})
    url = f"{API_BASE}{path}"
    if query:
        url = f"{url}?{query}"

    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "manifold-active-until-june-fetcher/1.0",
        },
    )

    for attempt in range(max_retries):
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            retryable = error.code == 429 or 500 <= error.code < 600
            if not retryable or attempt == max_retries - 1:
                raise

            retry_after = error.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else 2**attempt
            time.sleep(delay)
        except URLError:
            if attempt == max_retries - 1:
                raise
            time.sleep(2**attempt)

    raise RuntimeError("unreachable")


def millis_to_datetime(milliseconds: int, timezone: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone)


def is_matching_market(
    market: dict[str, Any],
    now_ms: int,
    cutoff_ms: int,
    recent_trade_start_ms: int,
) -> bool:
    close_time = market.get("closeTime")
    last_bet_time = market.get("lastBetTime")
    return (
        market.get("isResolved") is False
        and isinstance(close_time, int)
        and isinstance(last_bet_time, int)
        and now_ms <= close_time <= cutoff_ms
        and recent_trade_start_ms <= last_bet_time <= now_ms
    )


def scan_markets(
    now_ms: int,
    cutoff_ms: int,
    recent_trade_start_ms: int,
) -> tuple[list[dict[str, Any]], int, int]:
    matches: list[dict[str, Any]] = []
    total_seen = 0
    page_count = 0
    before: str | None = None

    while True:
        params: dict[str, Any] = {"limit": PAGE_LIMIT}
        if before:
            params["before"] = before

        page = fetch_json("/markets", params)
        if not isinstance(page, list):
            raise RuntimeError(f"Unexpected /markets response: {type(page).__name__}")
        if not page:
            break

        page_count += 1
        total_seen += len(page)
        matches.extend(
            market
            for market in page
            if is_matching_market(
                market,
                now_ms,
                cutoff_ms,
                recent_trade_start_ms,
            )
        )

        print(
            f"page={page_count} seen={total_seen} matches={len(matches)}",
            file=sys.stderr,
            flush=True,
        )

        if len(page) < PAGE_LIMIT:
            break

        before = page[-1]["id"]
        if PAGE_SLEEP_SECONDS:
            time.sleep(PAGE_SLEEP_SECONDS)

    return matches, total_seen, page_count


def enrich_with_descriptions(markets: list[dict[str, Any]]) -> None:
    total = len(markets)
    for index, market in enumerate(markets, start=1):
        market_id = market["id"]
        try:
            detail = fetch_json(f"/market/{market_id}")
            if not isinstance(detail, dict):
                raise RuntimeError(
                    f"Unexpected /market/{market_id} response: {type(detail).__name__}"
                )

            market["textDescription"] = detail.get("textDescription", "")
            market["descriptionError"] = ""
        except Exception as error:
            market["textDescription"] = ""
            market["descriptionError"] = f"{type(error).__name__}: {error}"

        if index == 1 or index % 50 == 0 or index == total:
            print(f"descriptions={index}/{total}", file=sys.stderr, flush=True)

        if DESCRIPTION_SLEEP_SECONDS:
            time.sleep(DESCRIPTION_SLEEP_SECONDS)


def write_csv(path: str, markets: list[dict[str, Any]], timezone: ZoneInfo) -> None:
    fields = [
        "id",
        "question",
        "url",
        "creatorUsername",
        "outcomeType",
        "createdTime",
        "createdDate",
        "closeTime",
        "closeDate",
        "lastBetTime",
        "lastBetDate",
        "probability",
        "volume",
        "volume24Hours",
        "uniqueBettorCount",
        "textDescription",
        "descriptionError",
    ]

    with open(path, "w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for market in markets:
            close_time = market["closeTime"]
            row = {field: market.get(field) for field in fields}
            row["closeDate"] = millis_to_datetime(close_time, timezone).isoformat()
            created_time = market.get("createdTime")
            if isinstance(created_time, int):
                row["createdDate"] = millis_to_datetime(
                    created_time,
                    timezone,
                ).isoformat()
            last_bet_time = market.get("lastBetTime")
            if isinstance(last_bet_time, int):
                row["lastBetDate"] = millis_to_datetime(
                    last_bet_time,
                    timezone,
                ).isoformat()
            writer.writerow(row)


def summarize(markets: list[dict[str, Any]], total_seen: int, pages: int) -> None:
    by_type = Counter(market.get("outcomeType", "UNKNOWN") for market in markets)
    by_token = Counter(market.get("token", "UNKNOWN") for market in markets)
    description_errors = sum(1 for market in markets if market.get("descriptionError"))
    non_empty_descriptions = sum(
        1 for market in markets if market.get("textDescription")
    )

    result = {
        "matchingActiveMarkets": len(markets),
        "marketsScanned": total_seen,
        "pagesScanned": pages,
        "tradeLookbackHours": TRADE_LOOKBACK_HOURS,
        "nonEmptyTextDescriptions": non_empty_descriptions,
        "descriptionErrors": description_errors,
        "byOutcomeType": dict(by_type.most_common()),
        "byToken": dict(by_token.most_common()),
        "outputCsv": OUTPUT_CSV,
    }
    print(json.dumps(result, indent=2, sort_keys=True))


def main() -> int:
    timezone = ZoneInfo(TIMEZONE)
    now = datetime.now(timezone)
    cutoff = datetime.combine(
        datetime(CUTOFF_YEAR, CUTOFF_MONTH, CUTOFF_DAY, tzinfo=timezone).date(),
        datetime_time.max,
        tzinfo=timezone,
    )

    if cutoff < now:
        print(
            f"Cutoff {cutoff.isoformat()} is before now {now.isoformat()}.",
            file=sys.stderr,
        )
        return 2

    now_ms = int(now.timestamp() * 1000)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    recent_trade_start_ms = now_ms - TRADE_LOOKBACK_HOURS * 60 * 60 * 1000

    print(f"now={now.isoformat()}", file=sys.stderr)
    print(f"cutoff={cutoff.isoformat()}", file=sys.stderr)
    print(
        "recent_trade_start="
        f"{millis_to_datetime(recent_trade_start_ms, timezone).isoformat()}",
        file=sys.stderr,
    )

    markets, total_seen, pages = scan_markets(
        now_ms,
        cutoff_ms,
        recent_trade_start_ms,
    )
    markets.sort(key=lambda market: market["closeTime"])
    enrich_with_descriptions(markets)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    write_csv(OUTPUT_CSV, markets, timezone)
    print(f"wrote_csv={OUTPUT_CSV}", file=sys.stderr)
    summarize(markets, total_seen, pages)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
