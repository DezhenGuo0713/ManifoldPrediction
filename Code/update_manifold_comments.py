#!/usr/bin/env python3
"""Create or update Manifold comments with static forecast card images.

This is the native-app fallback for forecast cards. Manifold's mobile app can
struggle with nested iframes, but API-created markdown comments become normal
Manifold comment content, including image nodes.

Setup:

    MANIFOLD_API_KEY=...

Dry run:

    python Code/update_manifold_comments.py --dry-run

Post or update comments:

    python Code/update_manifold_comments.py
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKETS_DIR = os.path.join(PROJECT_DIR, "Markets")
DOCS_DIR = os.path.join(PROJECT_DIR, "docs")
DEFAULT_INPUTS = [
    os.path.join(MARKETS_DIR, "MarketNewsPredictions.csv"),
    os.path.join(DOCS_DIR, "predictions.json"),
    os.path.join(MARKETS_DIR, "MarketNewsPredictions.10_sample.csv"),
    os.path.join(MARKETS_DIR, "MarketNewsPredictions.sample.csv"),
]
DEFAULT_MAPPING_PATH = os.path.join(MARKETS_DIR, "ManifoldPredictionComments.json")
DEFAULT_MANIFOLD_BASE = "https://api.manifold.markets/v0"
DEFAULT_IMAGE_BASE_URL = (
    "https://raw.githubusercontent.com/"
    "DezhenGuo0713/ManifoldPrediction/main/docs/cards"
)
DEFAULT_CARD_BASE_URL = "https://manifoldprediction.streamlit.app"
DISPLAY_TIMEZONE = ZoneInfo("America/New_York")
USER_AGENT = "manifold-prediction-comment-updater/1.0"


class ManifoldAPIError(RuntimeError):
    def __init__(self, status: int, path: str, details: str) -> None:
        super().__init__(f"Manifold HTTP {status} for {path}: {details[:800]}")
        self.status = status
        self.path = path
        self.details = details


@dataclass(frozen=True)
class ForecastCard:
    market_id: str
    question: str
    market_url: str
    yes_probability: float
    no_probability: float
    reason: str
    sources: list[str]
    timestamp: str


def parse_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def pct(value: float) -> str:
    return f"{round(value * 100)}%"


def markdown_alt(value: str) -> str:
    return (value or "").replace("[", "(").replace("]", ")").replace("\n", " ")


def split_pipe(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split("|") if part.strip()]


def format_timestamp(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=DISPLAY_TIMEZONE)
    return parsed.astimezone(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")


def resolve_input(path: str | None) -> str:
    if path:
        return path
    for candidate in DEFAULT_INPUTS:
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError("No prediction CSV or docs/predictions.json found.")


def load_cards(path: str) -> list[ForecastCard]:
    if path.lower().endswith(".json"):
        with open(path, encoding="utf-8") as input_file:
            payload = json.load(input_file)
        return [card_from_json(item) for item in payload if item.get("id")]

    with open(path, newline="", encoding="utf-8") as input_file:
        reader = csv.DictReader(input_file)
        return [card_from_csv(row) for row in reader if row.get("id")]


def card_from_json(item: dict[str, Any]) -> ForecastCard:
    yes = parse_float(item.get("yesProbability"))
    no = parse_float(item.get("noProbability"), 1 - yes)
    sources = item.get("sources")
    if not isinstance(sources, list):
        sources = []
    return ForecastCard(
        market_id=str(item.get("id", "")).strip(),
        question=str(item.get("question", "")).strip(),
        market_url=str(item.get("marketUrl", "")).strip(),
        yes_probability=yes,
        no_probability=no,
        reason=str(item.get("reason", "")).strip(),
        sources=[str(url).strip() for url in sources if str(url).strip()][:2],
        timestamp=format_timestamp(str(item.get("forecastTimestamp", ""))),
    )


def card_from_csv(row: dict[str, str]) -> ForecastCard:
    yes = parse_float(row.get("newsPredictedYesProbability"))
    no = parse_float(row.get("newsPredictedNoProbability"), 1 - yes)
    return ForecastCard(
        market_id=row.get("id", "").strip(),
        question=row.get("question", "").strip(),
        market_url=row.get("url", "").strip(),
        yes_probability=yes,
        no_probability=no,
        reason=row.get("newsShortReason", "").strip(),
        sources=split_pipe(row.get("newsSourceUrls", ""))[:2],
        timestamp=format_timestamp(row.get("forecastTimestamp", "")),
    )


def load_mapping(path: str) -> dict[str, dict[str, Any]]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as input_file:
        payload = json.load(input_file)
    comments = payload.get("comments", payload)
    normalized: dict[str, dict[str, Any]] = {}
    if isinstance(comments, dict):
        for market_id, entry in comments.items():
            if isinstance(entry, str):
                normalized[market_id] = {"commentId": entry}
            elif isinstance(entry, dict):
                normalized[market_id] = dict(entry)
    return normalized


def save_mapping(path: str, mapping: dict[str, dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "version": 1,
        "updatedAt": datetime.now(DISPLAY_TIMEZONE).isoformat(timespec="seconds"),
        "comments": dict(sorted(mapping.items())),
    }
    with open(path, "w", encoding="utf-8", newline="\n") as output_file:
        json.dump(payload, output_file, ensure_ascii=False, indent=2)
        output_file.write("\n")


def api_request(
    method: str,
    path: str,
    api_key: str,
    base_url: str,
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    max_retries: int = 4,
) -> Any:
    query = urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    if query:
        url = f"{url}?{query}"

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Authorization": f"Key {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )

    for attempt in range(max_retries):
        try:
            with urlopen(request, timeout=45) as response:
                body = response.read()
            return json.loads(body.decode("utf-8")) if body else {}
        except HTTPError as error:
            retryable = error.code == 429 or 500 <= error.code < 600
            if not retryable or attempt == max_retries - 1:
                details = error.read().decode("utf-8", errors="replace")
                raise ManifoldAPIError(error.code, path, details) from error
            retry_after = error.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else 2**attempt
            time.sleep(delay)
        except URLError as error:
            if attempt == max_retries - 1:
                raise RuntimeError(f"Manifold request failed for {path}: {error}") from error
            time.sleep(2**attempt)

    raise RuntimeError("unreachable")


def build_markdown(
    card: ForecastCard,
    image_base_url: str,
    card_base_url: str,
) -> str:
    image_url = f"{image_base_url.rstrip('/')}/{card.market_id}.png"
    card_url = (
        f"{card_base_url.rstrip('/')}/?market={card.market_id}"
        "&mode=embed&embedded=true"
    )
    source_links = "\n".join(
        f"{index}. <{url}>" for index, url in enumerate(card.sources[:2], start=1)
    )
    if not source_links:
        source_links = "No source URLs returned."

    return "\n".join(
        [
            f"![Forecast card for {markdown_alt(card.question)}]({image_url})",
            "",
            f"**YES {pct(card.yes_probability)} / NO {pct(card.no_probability)}**",
            f"Updated: {card.timestamp}",
            "",
            card.reason,
            "",
            "Sources:",
            source_links,
            "",
            f"Interactive card: <{card_url}>",
            "",
            f"`manifold-prediction-card:{card.market_id}`",
        ]
    )


def fetch_me(api_key: str, base_url: str) -> dict[str, Any]:
    me = api_request("GET", "me", api_key=api_key, base_url=base_url)
    if not isinstance(me, dict) or not me.get("id"):
        raise RuntimeError("Could not verify Manifold API key with /me.")
    return me


def find_existing_comment_id(
    card: ForecastCard,
    api_key: str,
    base_url: str,
    user_id: str,
) -> str | None:
    comments = api_request(
        "GET",
        "comments",
        api_key=api_key,
        base_url=base_url,
        params={"contractId": card.market_id, "limit": 1000, "order": "newest"},
    )
    marker = f"manifold-prediction-card:{card.market_id}"
    if not isinstance(comments, list):
        return None
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        if comment.get("userId") != user_id:
            continue
        comment_text = json.dumps(comment, ensure_ascii=False)
        if marker in comment_text and comment.get("id"):
            return str(comment["id"])
    return None


def upsert_comment(
    card: ForecastCard,
    markdown: str,
    mapping: dict[str, dict[str, Any]],
    api_key: str,
    base_url: str,
    user_id: str,
    allow_create: bool,
) -> str:
    entry = mapping.get(card.market_id, {})
    comment_id = str(entry.get("commentId", "")).strip()

    if not comment_id:
        comment_id = find_existing_comment_id(card, api_key, base_url, user_id) or ""

    if comment_id:
        try:
            api_request(
                "POST",
                "edit-comment",
                api_key=api_key,
                base_url=base_url,
                payload={
                    "contractId": card.market_id,
                    "commentId": comment_id,
                    "markdown": markdown,
                },
            )
            return comment_id
        except ManifoldAPIError as error:
            print(
                f"edit failed market={card.market_id} comment={comment_id}: {error}",
                file=sys.stderr,
            )
            if error.status != 404:
                raise

    if not allow_create:
        raise RuntimeError(f"No existing comment found for market {card.market_id}")

    result = api_request(
        "POST",
        "comment",
        api_key=api_key,
        base_url=base_url,
        payload={"contractId": card.market_id, "markdown": markdown},
    )
    if not isinstance(result, dict) or not result.get("id"):
        raise RuntimeError(f"Create comment response did not include an id: {result}")
    return str(result["id"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update Manifold market comments with forecast card images."
    )
    parser.add_argument(
        "--input",
        default=None,
        help="Prediction CSV or docs/predictions.json. Defaults to generated CSV, then JSON.",
    )
    parser.add_argument(
        "--mapping",
        default=DEFAULT_MAPPING_PATH,
        help=f"Market-to-comment id mapping. Default: {DEFAULT_MAPPING_PATH}",
    )
    parser.add_argument(
        "--market-id",
        action="append",
        default=None,
        help="Only update this market id. May be supplied multiple times.",
    )
    parser.add_argument(
        "--api-base-url",
        default=DEFAULT_MANIFOLD_BASE,
        help=f"Manifold API base URL. Default: {DEFAULT_MANIFOLD_BASE}",
    )
    parser.add_argument(
        "--image-base-url",
        default=DEFAULT_IMAGE_BASE_URL,
        help="Public base URL for generated PNG cards.",
    )
    parser.add_argument(
        "--card-base-url",
        default=DEFAULT_CARD_BASE_URL,
        help="Public base URL for the interactive Streamlit card.",
    )
    parser.add_argument(
        "--no-create",
        action="store_true",
        help="Only edit previously found comments; do not create new comments.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print markdown for the selected cards without calling Manifold.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = resolve_input(args.input)
    requested_ids = set(args.market_id or [])
    cards = [
        card
        for card in load_cards(input_path)
        if card.market_id and (not requested_ids or card.market_id in requested_ids)
    ]

    if not cards:
        print("No prediction cards matched the inputs.", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"input={input_path}")
        for card in cards:
            print("\n" + "=" * 80)
            print(build_markdown(card, args.image_base_url, args.card_base_url))
        return 0

    api_key = os.environ.get("MANIFOLD_API_KEY")
    if not api_key:
        print("MANIFOLD_API_KEY is required unless --dry-run is used.", file=sys.stderr)
        return 2

    me = fetch_me(api_key, args.api_base_url)
    mapping = load_mapping(args.mapping)
    updated = 0
    created_or_found = 0

    for card in cards:
        markdown = build_markdown(card, args.image_base_url, args.card_base_url)
        comment_id = upsert_comment(
            card=card,
            markdown=markdown,
            mapping=mapping,
            api_key=api_key,
            base_url=args.api_base_url,
            user_id=str(me["id"]),
            allow_create=not args.no_create,
        )
        previous_id = str(mapping.get(card.market_id, {}).get("commentId", ""))
        mapping[card.market_id] = {
            "commentId": comment_id,
            "marketUrl": card.market_url,
            "imageUrl": f"{args.image_base_url.rstrip('/')}/{card.market_id}.png",
            "lastUpdated": datetime.now(DISPLAY_TIMEZONE).isoformat(timespec="seconds"),
        }
        updated += 1
        if previous_id != comment_id:
            created_or_found += 1
        print(f"updated market={card.market_id} comment={comment_id}")

    save_mapping(args.mapping, mapping)
    print(
        json.dumps(
            {
                "input": input_path,
                "mapping": args.mapping,
                "updated": updated,
                "createdOrFound": created_or_found,
                "manifoldUser": me.get("username") or me.get("name") or me.get("id"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
