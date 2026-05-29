#!/usr/bin/env python3
"""Renew eligible Manifold markets, randomize new ones, and predict them.

This script is the repeatable pipeline for daily/periodic updates:

1. Fetch currently active markets using the project selection criteria.
2. Refresh Markets/active_markets_until_end_june_2026_with_descriptions.csv.
3. Find eligible unresolved BINARY markets with descriptions that are not already in
   Markets/MarketsRandomization.csv or Markets/new_markets.csv.
4. Randomize only genuinely new eligible markets into Treatment/Control while
   preserving existing assignments.
5. Predict pending rows from Markets/new_markets.csv and merge them into
   Markets/MarketNewsPredictions.csv.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import subprocess
import sys
import tempfile
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import manifold_active_until_june as active
import market_randomization as randomization
import monitor_new_markets as monitor
import post_embed_comment as poster
import predict_market_news as predictor


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKETS_DIR = os.path.join(PROJECT_DIR, "Markets")
PREDICTIONS_DIR = os.path.join(PROJECT_DIR, "Predictions")
ACTIVE_MARKETS_CSV = os.path.join(
    MARKETS_DIR,
    "active_markets_until_end_june_2026_with_descriptions.csv",
)
RANDOMIZED_MARKETS_CSV = os.path.join(MARKETS_DIR, "MarketsRandomization.csv")
NEW_MARKETS_CSV = os.path.join(MARKETS_DIR, "new_markets.csv")
MARKET_NEWS_PREDICTIONS_CSV = os.path.join(MARKETS_DIR, "MarketNewsPredictions.csv")
POST_LEDGER_CSV = os.path.join(MARKETS_DIR, "ManifoldPostLedger.csv")
PREDICT_SCRIPT = os.path.join(PROJECT_DIR, "Code", "predict_market_news.py")
RUN_TIMEZONE = ZoneInfo(active.TIMEZONE)
POST_LEDGER_COLUMNS = [
    "marketId",
    "marketUrl",
    "randomizationGroup",
    "embedUrl",
    "postStatus",
    "postedAt",
    "commentId",
    "commentResponse",
    "lastCheckedAt",
    "error",
]
POSTED_LEDGER_STATUSES = {
    "posted",
    "skipped_duplicate_existing_comment",
}


def read_csv_rows(path: str) -> tuple[list[dict[str, str]], list[str]]:
    if not os.path.exists(path):
        return [], []
    with open(path, newline="", encoding="utf-8") as input_file:
        reader = csv.DictReader(input_file)
        if reader.fieldnames is None:
            return [], []
        return list(reader), list(reader.fieldnames)


def write_csv_rows(
    path: str,
    rows: list[dict[str, str]],
    fieldnames: list[str],
) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def union_fieldnames(*fieldname_lists: list[str], rows: list[dict[str, str]] | None = None) -> list[str]:
    fieldnames: list[str] = []
    for fieldname_list in fieldname_lists:
        for fieldname in fieldname_list:
            if fieldname not in fieldnames:
                fieldnames.append(fieldname)
    for row in rows or []:
        for fieldname in row:
            if fieldname not in fieldnames:
                fieldnames.append(fieldname)
    return fieldnames


def ids_from_rows(rows: list[dict[str, str]]) -> set[str]:
    return {row["id"].strip() for row in rows if row.get("id", "").strip()}


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_datetime_value(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=RUN_TIMEZONE)
    return parsed.astimezone(RUN_TIMEZONE)


def row_close_datetime(row: dict[str, str]) -> datetime | None:
    return predictor.market_close_datetime(row)


def row_created_datetime(row: dict[str, str]) -> datetime | None:
    created_time = parse_float(row.get("createdTime"))
    if created_time is not None:
        timestamp = created_time / 1000 if created_time > 10_000_000_000 else created_time
        return datetime.fromtimestamp(timestamp, tz=RUN_TIMEZONE)
    return parse_datetime_value(row.get("createdDate", ""))


def is_row_closed(row: dict[str, str], now: datetime | None = None) -> bool:
    return predictor.is_market_closed(row, now or datetime.now(RUN_TIMEZONE))


def is_row_resolved(row: dict[str, str]) -> bool:
    value = row.get("isResolved", "").strip().lower()
    return value in {"true", "1", "yes"}


def refresh_market_runtime_status(row: dict[str, str]) -> dict[str, str]:
    market_id = row.get("id", "").strip()
    if not market_id:
        return row

    refreshed = dict(row)
    try:
        detail = active.fetch_json(f"/market/{market_id}")
    except Exception as error:
        refreshed["liveStatusError"] = f"{type(error).__name__}: {error}"
        return refreshed

    if not isinstance(detail, dict):
        refreshed["liveStatusError"] = f"Unexpected response: {type(detail).__name__}"
        return refreshed

    for field in ("isResolved", "resolution"):
        if field in detail:
            value = detail.get(field)
            refreshed[field] = "" if value is None else str(value)

    close_time = detail.get("closeTime")
    if isinstance(close_time, int):
        refreshed["closeTime"] = str(close_time)
        refreshed["closeDate"] = active.millis_to_datetime(close_time, RUN_TIMEZONE).isoformat()

    refreshed["liveStatusError"] = ""
    return refreshed


def refresh_prediction_runtime_statuses(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [refresh_market_runtime_status(row) for row in rows]


def close_sort_key(row: dict[str, str]) -> tuple[float, str]:
    close_time = row_close_datetime(row)
    timestamp = close_time.timestamp() if close_time is not None else float("inf")
    return timestamp, row.get("id", "")


def earliest_closing_rows(
    rows: list[dict[str, str]],
    limit: int | None,
    open_only: bool = False,
) -> list[dict[str, str]]:
    if limit is None:
        return rows
    candidate_rows = (
        [row for row in rows if not is_row_closed(row)] if open_only else rows
    )
    return sorted(candidate_rows, key=close_sort_key)[:limit]


def created_on_date(row: dict[str, str], target_date: date) -> bool:
    created_at = row_created_datetime(row)
    return created_at is not None and created_at.date() == target_date


def max_randomization_order(rows: list[dict[str, str]]) -> int:
    orders: list[int] = []
    for row in rows:
        try:
            orders.append(int(row.get("randomizationOrder", "")))
        except ValueError:
            continue
    return max(orders, default=0)


def group_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    return {
        randomization.TREATMENT_GROUP: sum(
            1
            for row in rows
            if row.get("randomizationGroup") == randomization.TREATMENT_GROUP
        ),
        randomization.CONTROL_GROUP: sum(
            1
            for row in rows
            if row.get("randomizationGroup") == randomization.CONTROL_GROUP
        ),
    }


def refresh_active_markets(active_output_csv: str, use_existing_active: bool) -> dict[str, Any]:
    if use_existing_active:
        rows, _ = read_csv_rows(active_output_csv)
        return {
            "activeRows": rows,
            "activeMatches": len(rows),
            "marketsScanned": 0,
            "pagesScanned": 0,
            "activeOutputCsv": active_output_csv,
            "usedExistingActiveCsv": True,
        }

    timezone = RUN_TIMEZONE
    now = datetime.now(timezone)
    matches, total_seen, pages = monitor.current_active_matches(now)
    matches.sort(key=lambda market: market["closeTime"])
    active.enrich_with_descriptions(matches)
    rows = [monitor.market_to_row(market, timezone) for market in matches]
    write_csv_rows(active_output_csv, rows, monitor.ACTIVE_FIELDS)
    return {
        "activeRows": rows,
        "activeMatches": len(rows),
        "marketsScanned": total_seen,
        "pagesScanned": pages,
        "activeOutputCsv": active_output_csv,
        "usedExistingActiveCsv": False,
    }


def balanced_incremental_randomize(
    new_rows: list[dict[str, str]],
    existing_assignment_rows: list[dict[str, str]],
    seed: int,
    discovered_at: str,
    checked_sources: list[str],
) -> list[dict[str, str]]:
    if not new_rows:
        return []

    rng = random.Random(f"{seed}:{','.join(sorted(row['id'] for row in new_rows))}")
    rows = [dict(row) for row in sorted(new_rows, key=lambda row: row["id"])]
    rng.shuffle(rows)

    counts = group_counts(existing_assignment_rows)
    next_order = max_randomization_order(existing_assignment_rows) + 1
    checked_text = " | ".join(checked_sources)

    for row in rows:
        treatment_count = counts[randomization.TREATMENT_GROUP]
        control_count = counts[randomization.CONTROL_GROUP]
        if treatment_count < control_count:
            group = randomization.TREATMENT_GROUP
        elif control_count < treatment_count:
            group = randomization.CONTROL_GROUP
        else:
            group = rng.choice(
                [randomization.TREATMENT_GROUP, randomization.CONTROL_GROUP]
            )

        row["randomizationGroup"] = group
        row["randomizationOrder"] = str(next_order)
        row["randomizationSeed"] = str(seed)
        row["monitorDiscoveredAt"] = discovered_at
        row["monitorExistingSourcesChecked"] = checked_text
        counts[group] += 1
        next_order += 1

    return rows


def update_new_markets(
    active_rows: list[dict[str, str]],
    new_markets_csv: str,
    randomized_markets_csv: str,
    seed: int,
    extra_existing_csvs: list[str],
    created_on: date | None,
    new_market_limit: int | None,
) -> dict[str, Any]:
    randomized_rows, _ = read_csv_rows(randomized_markets_csv)
    existing_new_rows, existing_new_fieldnames = read_csv_rows(new_markets_csv)
    extra_existing_rows: list[dict[str, str]] = []
    extra_existing_fieldnames: list[str] = []
    for path in extra_existing_csvs:
        rows, fieldnames = read_csv_rows(path)
        extra_existing_rows.extend(rows)
        extra_existing_fieldnames.extend(fieldnames)

    known_ids = (
        ids_from_rows(randomized_rows)
        | ids_from_rows(existing_new_rows)
        | ids_from_rows(extra_existing_rows)
    )

    eligible_active_rows = [
        row for row in active_rows if randomization.is_matching_market(row)
    ]
    if created_on is not None:
        eligible_active_rows = [
            row for row in eligible_active_rows if created_on_date(row, created_on)
        ]
    newly_eligible_rows = [
        row
        for row in eligible_active_rows
        if row.get("id", "").strip() and row["id"].strip() not in known_ids
    ]
    newly_eligible_rows.sort(key=close_sort_key)
    selected_newly_eligible_rows = earliest_closing_rows(
        newly_eligible_rows,
        new_market_limit,
    )

    discovered_at = datetime.now(RUN_TIMEZONE).replace(microsecond=0).isoformat()
    checked_sources = [randomized_markets_csv, new_markets_csv, *extra_existing_csvs]
    randomized_new_rows = balanced_incremental_randomize(
        selected_newly_eligible_rows,
        randomized_rows + existing_new_rows + extra_existing_rows,
        seed,
        discovered_at,
        checked_sources,
    )

    output_rows = existing_new_rows + randomized_new_rows
    output_fieldnames = union_fieldnames(
        existing_new_fieldnames,
        extra_existing_fieldnames,
        monitor.ACTIVE_FIELDS,
        randomization.RANDOMIZATION_COLUMNS,
        monitor.MONITOR_COLUMNS,
        rows=output_rows,
    )
    write_csv_rows(new_markets_csv, output_rows, output_fieldnames)

    return {
        "newMarketsCsv": new_markets_csv,
        "eligibleActiveMarkets": len(eligible_active_rows),
        "knownRandomizedOrNewIds": len(known_ids),
        "newEligibleMarketsAvailableThisRun": len(newly_eligible_rows),
        "newEligibleMarketsThisRun": len(selected_newly_eligible_rows),
        "newRowsRandomizedThisRun": len(randomized_new_rows),
        "newMarketsTotalRows": len(output_rows),
        "groupCountsAllKnown": group_counts(randomized_rows + output_rows + extra_existing_rows),
        "groupCountsThisRun": group_counts(randomized_new_rows),
        "newMarketIdsThisRun": [row["id"] for row in randomized_new_rows],
        "createdOnDate": created_on.isoformat() if created_on else "",
        "newMarketLimit": new_market_limit,
        "extraExistingCsvs": extra_existing_csvs,
    }


def pool_market_rows(
    randomized_markets_csv: str,
    new_markets_csv: str,
) -> tuple[list[dict[str, str]], list[str]]:
    randomized_rows, randomized_fieldnames = read_csv_rows(randomized_markets_csv)
    new_rows, new_fieldnames = read_csv_rows(new_markets_csv)
    rows_by_id: dict[str, dict[str, str]] = {}
    for row in randomized_rows + new_rows:
        market_id = row.get("id", "").strip()
        if market_id and market_id not in rows_by_id:
            rows_by_id[market_id] = row
    rows = list(rows_by_id.values())
    fieldnames = union_fieldnames(randomized_fieldnames, new_fieldnames, rows=rows)
    return rows, fieldnames


def prediction_input_rows(
    randomized_markets_csv: str,
    new_markets_csv: str,
    predictions_csv: str,
    scope: str,
    new_market_ids_this_run: set[str],
    earliest_close_limit: int | None,
) -> tuple[list[dict[str, str]], list[str]]:
    new_rows, new_fieldnames = read_csv_rows(new_markets_csv)
    predicted_rows, _ = read_csv_rows(predictions_csv)
    predicted_ids = ids_from_rows(predicted_rows)

    if scope == "this-run":
        pending_rows = [
            row for row in new_rows if row.get("id", "").strip() in new_market_ids_this_run
        ]
    elif scope == "all-new-markets":
        pending_rows = list(new_rows)
    elif scope == "all-pool":
        pending_rows, new_fieldnames = pool_market_rows(
            randomized_markets_csv,
            new_markets_csv,
        )
    elif scope == "pending-pool":
        pool_rows, new_fieldnames = pool_market_rows(
            randomized_markets_csv,
            new_markets_csv,
        )
        pending_rows = [
            row
            for row in pool_rows
            if row.get("id", "").strip() and row["id"].strip() not in predicted_ids
        ]
    else:
        pending_rows = [
            row
            for row in new_rows
            if row.get("id", "").strip() and row["id"].strip() not in predicted_ids
        ]

    return (
        earliest_closing_rows(
            pending_rows,
            earliest_close_limit,
            open_only=earliest_close_limit is not None,
        ),
        new_fieldnames,
    )


def merge_prediction_rows(
    predictions_csv: str,
    new_prediction_rows: list[dict[str, str]],
    new_prediction_fieldnames: list[str],
) -> int:
    if not new_prediction_rows:
        return 0

    existing_rows, existing_fieldnames = read_csv_rows(predictions_csv)
    new_ids = ids_from_rows(new_prediction_rows)
    merged_rows = [
        row for row in existing_rows if row.get("id", "").strip() not in new_ids
    ] + new_prediction_rows
    fieldnames = union_fieldnames(existing_fieldnames, new_prediction_fieldnames, rows=merged_rows)
    write_csv_rows(predictions_csv, merged_rows, fieldnames)
    return len(merged_rows)


def run_predictions(
    pending_rows: list[dict[str, str]],
    pending_fieldnames: list[str],
    predictions_dir: str,
    market_predictions_csv: str,
    current_date: str | None,
) -> dict[str, Any]:
    os.makedirs(predictions_dir, exist_ok=True)
    run_timestamp = datetime.now(RUN_TIMEZONE).strftime("%Y%m%dT%H%M%S%z")
    prediction_run_csv = os.path.join(
        predictions_dir,
        f"{run_timestamp}_renewed_market_predictions.csv",
    )

    if not pending_rows:
        fieldnames = union_fieldnames(pending_fieldnames, predictor.OUTPUT_COLUMNS)
        write_csv_rows(prediction_run_csv, [], fieldnames)
        return {
            "predictionRowsRequested": 0,
            "predictionRowsWrittenThisRun": 0,
            "predictionRunCsv": prediction_run_csv,
            "marketPredictionsCsv": market_predictions_csv,
            "marketPredictionsRowsTotal": len(read_csv_rows(market_predictions_csv)[0]),
        }

    with tempfile.NamedTemporaryFile(
        "w",
        newline="",
        encoding="utf-8",
        suffix=".csv",
        delete=False,
    ) as temp_file:
        temp_input_csv = temp_file.name
        fieldnames = union_fieldnames(pending_fieldnames, rows=pending_rows)
        writer = csv.DictWriter(temp_file, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(pending_rows)

    command = [
        sys.executable,
        PREDICT_SCRIPT,
        "--input",
        temp_input_csv,
        "--output",
        prediction_run_csv,
        "--no-history",
    ]
    if current_date:
        command.extend(["--current-date", current_date])

    try:
        subprocess.run(command, cwd=PROJECT_DIR, check=True)
    finally:
        try:
            os.unlink(temp_input_csv)
        except FileNotFoundError:
            pass

    new_prediction_rows, new_prediction_fieldnames = read_csv_rows(prediction_run_csv)
    total_rows = merge_prediction_rows(
        market_predictions_csv,
        new_prediction_rows,
        new_prediction_fieldnames,
    )
    return {
        "predictionRowsRequested": len(pending_rows),
        "predictionRowsWrittenThisRun": len(new_prediction_rows),
        "predictionRunCsv": prediction_run_csv,
        "marketPredictionsCsv": market_predictions_csv,
        "marketPredictionsRowsTotal": total_rows,
        "predictedIdsThisRun": [row["id"] for row in new_prediction_rows],
    }


def ledger_posted_ids(ledger_rows: list[dict[str, str]]) -> set[str]:
    return {
        row.get("marketId", "").strip()
        for row in ledger_rows
        if row.get("marketId", "").strip()
        and row.get("postStatus", "").strip() in POSTED_LEDGER_STATUSES
    }


def response_comment_id(response: Any) -> str:
    if isinstance(response, dict):
        for key in ("id", "commentId"):
            value = response.get(key)
            if value is not None:
                return str(value)
        comment = response.get("comment")
        if isinstance(comment, dict):
            value = comment.get("id")
            if value is not None:
                return str(value)
    return ""


def build_post_ledger_row(
    row: dict[str, str],
    embed_url: str,
    status: str,
    checked_at: str,
    response: Any = None,
    error: str = "",
) -> dict[str, str]:
    posted_at = checked_at if status in POSTED_LEDGER_STATUSES else ""
    return {
        "marketId": row.get("id", "").strip(),
        "marketUrl": row.get("url", "").strip(),
        "randomizationGroup": row.get("randomizationGroup", "").strip(),
        "embedUrl": embed_url,
        "postStatus": status,
        "postedAt": posted_at,
        "commentId": response_comment_id(response),
        "commentResponse": json.dumps(response or {}, ensure_ascii=False),
        "lastCheckedAt": checked_at,
        "error": error,
    }


def comment_candidate_rows(
    randomized_markets_csv: str,
    new_markets_csv: str,
    scope: str,
    new_market_ids_this_run: set[str],
    predicted_market_ids_this_run: set[str],
) -> list[dict[str, str]]:
    if scope == "none":
        return []
    pool_rows, _ = pool_market_rows(randomized_markets_csv, new_markets_csv)
    rows = [
        row
        for row in pool_rows
        if row.get("randomizationGroup") == randomization.TREATMENT_GROUP
    ]
    if scope == "new-treatment":
        rows = [
            row for row in rows if row.get("id", "").strip() in new_market_ids_this_run
        ]
    elif scope == "predicted-treatment":
        rows = [
            row
            for row in rows
            if row.get("id", "").strip() in predicted_market_ids_this_run
        ]
    elif scope != "all-unposted-treatment":
        raise ValueError(f"Unknown post scope: {scope}")
    return rows


def post_treatment_comments(
    candidate_rows: list[dict[str, str]],
    ledger_csv: str,
    post_comments: bool,
    site_base_url: str,
    streamlit_path: str,
    prefix: str,
    markdown_link: bool,
) -> dict[str, Any]:
    ledger_rows, ledger_fieldnames = read_csv_rows(ledger_csv)
    posted_ids = ledger_posted_ids(ledger_rows)
    checked_at = datetime.now(RUN_TIMEZONE).replace(microsecond=0).isoformat()
    new_ledger_rows: list[dict[str, str]] = []
    counts: dict[str, int] = {}

    for row in candidate_rows:
        market_id = row.get("id", "").strip()
        if not market_id:
            continue

        embed_url = poster.generated_market_url(
            market_id,
            site_base_url,
            streamlit_path,
        )

        if market_id in posted_ids:
            status = "skipped_ledger_already_posted"
            counts[status] = counts.get(status, 0) + 1
            continue

        row = refresh_market_runtime_status(row)
        if is_row_resolved(row):
            status = "skipped_resolved"
            new_ledger_rows.append(
                build_post_ledger_row(row, embed_url, status, checked_at)
            )
            counts[status] = counts.get(status, 0) + 1
            continue

        if is_row_closed(row):
            status = "skipped_closed"
            new_ledger_rows.append(
                build_post_ledger_row(row, embed_url, status, checked_at)
            )
            counts[status] = counts.get(status, 0) + 1
            continue

        if not post_comments:
            status = "dry_run"
            new_ledger_rows.append(
                build_post_ledger_row(row, embed_url, status, checked_at)
            )
            counts[status] = counts.get(status, 0) + 1
            continue

        try:
            duplicate_exists = poster.existing_comment_has_url(
                market_id,
                embed_url,
                require_iframe=not markdown_link,
            )
            if duplicate_exists:
                status = "skipped_duplicate_existing_comment"
                response: Any = {}
            else:
                markdown = poster.comment_markdown(embed_url, prefix)
                content = poster.iframe_comment_content(embed_url, prefix)
                payload = (
                    {"contractId": market_id, "markdown": markdown}
                    if markdown_link
                    else {"contractId": market_id, "content": content}
                )
                api_key = os.environ.get("MANIFOLD_API_KEY")
                if not api_key:
                    raise RuntimeError("MANIFOLD_API_KEY is required to post comments.")
                response = poster.post_json("/comment", payload, api_key)
                status = "posted"
            new_ledger_rows.append(
                build_post_ledger_row(
                    row,
                    embed_url,
                    status,
                    checked_at,
                    response=response,
                )
            )
            counts[status] = counts.get(status, 0) + 1
        except Exception as error:
            status = "error"
            new_ledger_rows.append(
                build_post_ledger_row(
                    row,
                    embed_url,
                    status,
                    checked_at,
                    error=f"{type(error).__name__}: {error}",
                )
            )
            counts[status] = counts.get(status, 0) + 1

    output_rows = ledger_rows + new_ledger_rows
    output_fieldnames = union_fieldnames(
        ledger_fieldnames,
        POST_LEDGER_COLUMNS,
        rows=output_rows,
    )
    write_csv_rows(ledger_csv, output_rows, output_fieldnames)

    return {
        "postLedgerCsv": ledger_csv,
        "postComments": post_comments,
        "candidateTreatmentRows": len(candidate_rows),
        "newLedgerRows": len(new_ledger_rows),
        "statusCounts": counts,
        "embedBaseUrl": site_base_url,
        "streamlitPath": streamlit_path,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Refresh active Manifold markets, incrementally randomize new "
            "eligible markets, and update news-only predictions."
        )
    )
    parser.add_argument("--active-output", default=ACTIVE_MARKETS_CSV)
    parser.add_argument("--new-markets-output", default=NEW_MARKETS_CSV)
    parser.add_argument("--randomized-markets", default=RANDOMIZED_MARKETS_CSV)
    parser.add_argument("--market-predictions-output", default=MARKET_NEWS_PREDICTIONS_CSV)
    parser.add_argument("--predictions-dir", default=PREDICTIONS_DIR)
    parser.add_argument("--post-ledger", default=POST_LEDGER_CSV)
    parser.add_argument(
        "--seed",
        type=int,
        default=randomization.DEFAULT_RANDOMIZATION_SEED,
        help=f"Randomization seed. Default: {randomization.DEFAULT_RANDOMIZATION_SEED}.",
    )
    parser.add_argument(
        "--prediction-scope",
        choices=[
            "pending-new-markets",
            "this-run",
            "all-new-markets",
            "pending-pool",
            "all-pool",
        ],
        default="pending-new-markets",
        help=(
            "Which rows to predict. Use all-pool for the twice-daily refresh. "
            "Default predicts new market rows that are not already in "
            "MarketNewsPredictions.csv."
        ),
    )
    parser.add_argument(
        "--skip-active-refresh",
        action="store_true",
        help="Do not fetch/refresh active markets. Use this for prediction-only refreshes.",
    )
    parser.add_argument(
        "--use-existing-active",
        action="store_true",
        help="Use --active-output as the active-market input instead of fetching Manifold.",
    )
    parser.add_argument(
        "--skip-new-market-intake",
        action="store_true",
        help="Do not append newly eligible markets to new_markets.csv.",
    )
    parser.add_argument(
        "--extra-existing-market-csv",
        action="append",
        default=None,
        help=(
            "Additional CSV whose id column should be excluded from new-market "
            "intake. Useful for tests that write to a temporary new_markets file."
        ),
    )
    parser.add_argument(
        "--new-markets-created-today",
        action="store_true",
        help="Only intake markets whose createdTime falls on today's America/New_York date.",
    )
    parser.add_argument(
        "--new-markets-created-on-date",
        default=None,
        help="Only intake markets created on this YYYY-MM-DD America/New_York date.",
    )
    parser.add_argument(
        "--test-new-market-limit",
        type=int,
        default=None,
        help="For controlled tests, only intake the N earliest-closing newly eligible markets.",
    )
    parser.add_argument(
        "--test-prediction-limit-earliest-close",
        type=int,
        default=None,
        help="For controlled tests, only predict the N earliest-closing rows in scope.",
    )
    parser.add_argument(
        "--skip-predictions",
        action="store_true",
        help="Refresh and randomize markets without calling the OpenAI API.",
    )
    parser.add_argument(
        "--post-scope",
        choices=[
            "none",
            "new-treatment",
            "predicted-treatment",
            "all-unposted-treatment",
        ],
        default="none",
        help=(
            "Which Treatment markets should receive Manifold comments. Default "
            "does not post or dry-run comments."
        ),
    )
    parser.add_argument(
        "--post-comments",
        action="store_true",
        help="Actually post Manifold comments. Without this, post-scope writes dry-run ledger rows.",
    )
    parser.add_argument(
        "--site-base-url",
        default=poster.DEFAULT_SITE_BASE_URL,
        help=f"Streamlit app base URL. Default: {poster.DEFAULT_SITE_BASE_URL}",
    )
    parser.add_argument(
        "--streamlit-path",
        default=poster.DEFAULT_STREAMLIT_DIRECT_PATH,
        help=f"Streamlit direct path. Default: {poster.DEFAULT_STREAMLIT_DIRECT_PATH}",
    )
    parser.add_argument(
        "--comment-prefix",
        default="AI forecast card:",
        help="Text placed before the embedded card comment.",
    )
    parser.add_argument(
        "--markdown-link",
        action="store_true",
        help="Post a markdown link instead of a TipTap iframe embed.",
    )
    parser.add_argument(
        "--current-date",
        default=None,
        help="Override current date/time sent to the prediction model.",
    )
    return parser.parse_args()


def requested_created_on_date(args: argparse.Namespace) -> date | None:
    if args.new_markets_created_today and args.new_markets_created_on_date:
        raise ValueError(
            "Use either --new-markets-created-today or --new-markets-created-on-date, not both."
        )
    if args.new_markets_created_today:
        return datetime.now(RUN_TIMEZONE).date()
    if args.new_markets_created_on_date:
        return date.fromisoformat(args.new_markets_created_on_date)
    return None


def main() -> int:
    args = parse_args()
    try:
        created_on = requested_created_on_date(args)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    if args.skip_active_refresh:
        active_result = {
            "activeRows": [],
            "activeMatches": 0,
            "marketsScanned": 0,
            "pagesScanned": 0,
            "activeOutputCsv": args.active_output,
            "skippedActiveRefresh": True,
        }
    else:
        active_result = refresh_active_markets(
            args.active_output,
            use_existing_active=args.use_existing_active,
        )

    if args.skip_new_market_intake:
        randomization_result = {
            "skippedNewMarketIntake": True,
            "newMarketIdsThisRun": [],
        }
    else:
        randomization_result = update_new_markets(
            active_rows=active_result["activeRows"],
            new_markets_csv=args.new_markets_output,
            randomized_markets_csv=args.randomized_markets,
            seed=args.seed,
            extra_existing_csvs=args.extra_existing_market_csv or [],
            created_on=created_on,
            new_market_limit=args.test_new_market_limit,
        )

    prediction_result: dict[str, Any]
    if args.skip_predictions:
        prediction_result = {"skippedPredictions": True}
    else:
        pending_rows, pending_fieldnames = prediction_input_rows(
            randomized_markets_csv=args.randomized_markets,
            new_markets_csv=args.new_markets_output,
            predictions_csv=args.market_predictions_output,
            scope=args.prediction_scope,
            new_market_ids_this_run=set(randomization_result["newMarketIdsThisRun"]),
            earliest_close_limit=args.test_prediction_limit_earliest_close,
        )
        pending_rows = refresh_prediction_runtime_statuses(pending_rows)
        prediction_result = run_predictions(
            pending_rows=pending_rows,
            pending_fieldnames=pending_fieldnames,
            predictions_dir=args.predictions_dir,
            market_predictions_csv=args.market_predictions_output,
            current_date=args.current_date,
        )

    candidate_rows = comment_candidate_rows(
        randomized_markets_csv=args.randomized_markets,
        new_markets_csv=args.new_markets_output,
        scope=args.post_scope,
        new_market_ids_this_run=set(randomization_result["newMarketIdsThisRun"]),
        predicted_market_ids_this_run=set(
            prediction_result.get("predictedIdsThisRun", [])
        ),
    )
    post_result = post_treatment_comments(
        candidate_rows=candidate_rows,
        ledger_csv=args.post_ledger,
        post_comments=args.post_comments,
        site_base_url=args.site_base_url,
        streamlit_path=args.streamlit_path,
        prefix=args.comment_prefix,
        markdown_link=args.markdown_link,
    )

    summary = {
        "active": {key: value for key, value in active_result.items() if key != "activeRows"},
        "randomization": randomization_result,
        "prediction": prediction_result,
        "posting": post_result,
        "criteria": {
            "active": {
                "unresolved": True,
                "closesBy": (
                    f"{active.CUTOFF_YEAR:04d}-"
                    f"{active.CUTOFF_MONTH:02d}-"
                    f"{active.CUTOFF_DAY:02d}"
                ),
                "recentTradeLookbackHours": active.TRADE_LOOKBACK_HOURS,
            },
            "randomization": {
                "outcomeType": randomization.REQUIRED_OUTCOME_TYPE,
                "requiresDescription": True,
                "unresolved": True,
                "seed": args.seed,
                "incrementalBalance": True,
            },
            "newMarketIntake": {
                "createdOnDate": created_on.isoformat() if created_on else "",
                "testNewMarketLimit": args.test_new_market_limit,
            },
            "prediction": {
                "scope": args.prediction_scope,
                "testEarliestCloseLimit": args.test_prediction_limit_earliest_close,
            },
            "posting": {
                "scope": args.post_scope,
                "onlyTreatment": True,
                "oneLedgerPostedStatusPerMarket": True,
                "actualPost": args.post_comments,
            },
        },
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
