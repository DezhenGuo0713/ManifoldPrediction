#!/usr/bin/env python3
"""Monitor Manifold for newly eligible markets and randomize them.

The monitor uses the same active-market criteria as manifold_active_until_june:

    - unresolved
    - close time from now through 2026-06-30 23:59:59 America/New_York
    - at least one bet during the configured recent trade lookback window

It then applies the same randomization eligibility criteria as
market_randomization:

    - BINARY outcomeType
    - non-empty textDescription

Markets already present in the existing project market files are excluded by id.
New eligible markets are appended to Markets/new_markets.csv with randomized
Treatment/Control assignment.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime, time as datetime_time
from typing import Any
from zoneinfo import ZoneInfo

import manifold_active_until_june as active
import market_randomization as randomization


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKETS_DIR = os.path.join(PROJECT_DIR, "Markets")
ACTIVE_MARKETS_CSV = os.path.join(
    MARKETS_DIR,
    "active_markets_until_end_june_2026_with_descriptions.csv",
)
RANDOMIZED_MARKETS_CSV = os.path.join(MARKETS_DIR, "MarketsRandomization.csv")
DEFAULT_OUTPUT_CSV = os.path.join(MARKETS_DIR, "new_markets.csv")
DEFAULT_RANDOMIZATION_SEED = randomization.DEFAULT_RANDOMIZATION_SEED

ACTIVE_FIELDS = [
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
MONITOR_COLUMNS = [
    "monitorDiscoveredAt",
    "monitorExistingSourcesChecked",
]


def read_csv_rows(path: str) -> tuple[list[dict[str, str]], list[str]]:
    if not os.path.exists(path):
        return [], []

    with open(path, newline="", encoding="utf-8") as input_file:
        reader = csv.DictReader(input_file)
        if reader.fieldnames is None:
            return [], []
        return list(reader), list(reader.fieldnames)


def market_ids_from_csv(path: str) -> set[str]:
    rows, _ = read_csv_rows(path)
    return {row["id"].strip() for row in rows if row.get("id", "").strip()}


def existing_market_ids(paths: list[str]) -> tuple[set[str], list[str]]:
    ids: set[str] = set()
    checked_paths: list[str] = []
    for path in paths:
        checked_paths.append(path)
        ids.update(market_ids_from_csv(path))
    return ids, checked_paths


def market_to_row(market: dict[str, Any], timezone: ZoneInfo) -> dict[str, str]:
    row = {field: "" for field in ACTIVE_FIELDS}
    for field in ACTIVE_FIELDS:
        value = market.get(field)
        row[field] = "" if value is None else str(value)

    close_time = market.get("closeTime")
    if isinstance(close_time, int):
        row["closeDate"] = active.millis_to_datetime(close_time, timezone).isoformat()

    created_time = market.get("createdTime")
    if isinstance(created_time, int):
        row["createdDate"] = active.millis_to_datetime(
            created_time,
            timezone,
        ).isoformat()

    last_bet_time = market.get("lastBetTime")
    if isinstance(last_bet_time, int):
        row["lastBetDate"] = active.millis_to_datetime(
            last_bet_time,
            timezone,
        ).isoformat()

    return row


def current_active_matches(now: datetime) -> tuple[list[dict[str, Any]], int, int]:
    cutoff = datetime.combine(
        datetime(
            active.CUTOFF_YEAR,
            active.CUTOFF_MONTH,
            active.CUTOFF_DAY,
            tzinfo=now.tzinfo,
        ).date(),
        datetime_time.max,
        tzinfo=now.tzinfo,
    )
    if cutoff < now:
        raise RuntimeError(
            f"Cutoff {cutoff.isoformat()} is before now {now.isoformat()}."
        )

    now_ms = int(now.timestamp() * 1000)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    recent_trade_start_ms = now_ms - active.TRADE_LOOKBACK_HOURS * 60 * 60 * 1000
    return active.scan_markets(now_ms, cutoff_ms, recent_trade_start_ms)


def output_fieldnames(existing_fieldnames: list[str]) -> list[str]:
    fieldnames = list(existing_fieldnames) if existing_fieldnames else list(ACTIVE_FIELDS)
    for column in randomization.RANDOMIZATION_COLUMNS + MONITOR_COLUMNS:
        if column not in fieldnames:
            fieldnames.append(column)
    return fieldnames


def next_randomization_order(existing_rows: list[dict[str, str]]) -> int:
    orders: list[int] = []
    for row in existing_rows:
        try:
            orders.append(int(row.get("randomizationOrder", "")))
        except ValueError:
            continue
    return max(orders, default=0) + 1


def append_new_markets(
    output_csv: str,
    existing_rows: list[dict[str, str]],
    existing_fieldnames: list[str],
    new_rows: list[dict[str, str]],
) -> None:
    fieldnames = output_fieldnames(existing_fieldnames)
    rows = existing_rows + new_rows
    os.makedirs(os.path.dirname(output_csv), exist_ok=True)
    with open(output_csv, "w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def monitor_new_markets(
    output_csv: str,
    existing_csvs: list[str],
    seed: int,
    append_existing_output: bool,
) -> dict[str, Any]:
    timezone = ZoneInfo(active.TIMEZONE)
    now = datetime.now(timezone)
    discovered_at = now.replace(microsecond=0).isoformat()

    output_rows, output_fieldnames_existing = read_csv_rows(output_csv)
    previous_output_ids = {row["id"].strip() for row in output_rows if row.get("id", "").strip()}

    exclusion_paths = list(existing_csvs)
    if append_existing_output:
        exclusion_paths.append(output_csv)
    excluded_ids, checked_paths = existing_market_ids(exclusion_paths)

    matches, total_seen, pages = current_active_matches(now)
    matches.sort(key=lambda market: market["closeTime"])
    unseen_matches = [
        market
        for market in matches
        if str(market.get("id", "")).strip()
        and str(market.get("id", "")).strip() not in excluded_ids
    ]

    active.enrich_with_descriptions(unseen_matches)
    candidate_rows = [market_to_row(market, timezone) for market in unseen_matches]
    eligible_rows = [
        row for row in candidate_rows if randomization.is_matching_market(row)
    ]

    randomized_rows = randomization.randomize_markets(eligible_rows, seed)
    order_offset = next_randomization_order(output_rows)
    checked_text = " | ".join(checked_paths)
    for index, row in enumerate(randomized_rows, start=order_offset):
        row["randomizationOrder"] = str(index)
        row["monitorDiscoveredAt"] = discovered_at
        row["monitorExistingSourcesChecked"] = checked_text

    rows_to_keep = output_rows if append_existing_output else []
    fieldnames_to_keep = output_fieldnames_existing if append_existing_output else []
    append_new_markets(
        output_csv=output_csv,
        existing_rows=rows_to_keep,
        existing_fieldnames=fieldnames_to_keep,
        new_rows=randomized_rows,
    )

    return {
        "outputCsv": output_csv,
        "randomizationSeed": seed,
        "marketsScanned": total_seen,
        "pagesScanned": pages,
        "activeMatches": len(matches),
        "excludedExistingIds": len(excluded_ids),
        "previousNewMarketIds": len(previous_output_ids),
        "unseenActiveMatches": len(unseen_matches),
        "eligibleNewMarkets": len(eligible_rows),
        "newRowsWrittenThisRun": len(randomized_rows),
        "outputRowsTotal": len(rows_to_keep) + len(randomized_rows),
        "groupCountsThisRun": randomization.group_counts(randomized_rows),
        "criteria": {
            "activeMarketCriteria": {
                "unresolved": True,
                "closesBy": f"{active.CUTOFF_YEAR:04d}-{active.CUTOFF_MONTH:02d}-{active.CUTOFF_DAY:02d}",
                "recentTradeLookbackHours": active.TRADE_LOOKBACK_HOURS,
            },
            "randomizationCriteria": {
                "outcomeType": randomization.REQUIRED_OUTCOME_TYPE,
                "requiresDescription": True,
            },
            "excludedCsvs": checked_paths,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find active Manifold markets that match project criteria, exclude "
            "already-known ids, randomize the new eligible markets, and write "
            "Markets/new_markets.csv."
        )
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_CSV,
        help=f"Output CSV path. Default: {DEFAULT_OUTPUT_CSV}",
    )
    parser.add_argument(
        "--existing-csv",
        action="append",
        default=None,
        help=(
            "CSV whose id column should be excluded. May be supplied multiple "
            "times. Defaults to the active markets CSV and MarketsRandomization.csv."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_RANDOMIZATION_SEED,
        help=f"Randomization seed. Default: {DEFAULT_RANDOMIZATION_SEED}.",
    )
    parser.add_argument(
        "--replace-output",
        action="store_true",
        help="Replace new_markets.csv instead of appending to existing rows.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    existing_csvs = args.existing_csv or [
        ACTIVE_MARKETS_CSV,
        RANDOMIZED_MARKETS_CSV,
    ]
    summary = monitor_new_markets(
        output_csv=args.output,
        existing_csvs=existing_csvs,
        seed=args.seed,
        append_existing_output=not args.replace_output,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
