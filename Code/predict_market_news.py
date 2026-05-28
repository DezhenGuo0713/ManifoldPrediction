#!/usr/bin/env python3
"""Forecast Manifold binary markets from web-search evidence.

The final YES probability is the model's web-search forecast from question,
description, current date, and market closing date. Current Manifold probability
and no-search prior beliefs are not used as prediction inputs or blend signals.

Setup:

    $env:OPENAI_API_KEY = "..."

Run a small sample:

    python Code/predict_market_news.py --limit 5

Run all randomized binary markets:

    python Code/predict_market_news.py

Run one market:

    python Code/predict_market_news.py --market-id 5pnhA0q8pp --fetch-live-description

Environment overrides:

    MARKET_NEWS_MODEL      model name, default gpt-4.1-mini
    MARKET_NEWS_WEB_TOOL   web search tool, default web_search
    OPENAI_BASE_URL        API base URL, default https://api.openai.com
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKETS_DIR = os.path.join(PROJECT_DIR, "Markets")
DEFAULT_INPUT_CSV = os.path.join(MARKETS_DIR, "MarketsRandomization.csv")
DEFAULT_OUTPUT_CSV = os.path.join(MARKETS_DIR, "MarketNewsPredictions.csv")
DEFAULT_HISTORY_DIR = os.path.join(PROJECT_DIR, "Predictions")

MANIFOLD_API_BASE = "https://api.manifold.markets/v0"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com"
DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_WEB_TOOL = "web_search"
DEFAULT_MAX_SOURCE_URLS = 8
DEFAULT_BLOCKED_DOMAINS = [
    "manifold.markets",
    "polymarket.com",
    "kalshi.com",
    "predictit.org",
    "metaculus.com",
]
TIMEZONE = "America/New_York"
USER_AGENT = "manifold-news-forecaster/1.0"
MIN_PROBABILITY = 0.01
MAX_PROBABILITY = 0.99

OUTPUT_COLUMNS = [
    "forecastTimestamp",
    "forecastCurrentDate",
    "forecastModel",
    "forecastInputPolicy",
    "forecastStatus",
    "forecastClosedAt",
    "finalPredictedYesProbability",
    "finalPredictedNoProbability",
    "finalShortReason",
    "searchPredictedYesProbability",
    "searchPredictedNoProbability",
    "searchConfidence",
    "searchShortReason",
    "ensembleSearchWeight",
    "ensembleMethod",
    "newsPredictedYesProbability",
    "newsPredictedNoProbability",
    "newsConfidence",
    "newsShortReason",
    "newsKeyEvidence",
    "newsSourceUrls",
    "newsSearchQueries",
    "newsRawJson",
]


@dataclass(frozen=True)
class Forecast:
    yes_probability: float
    confidence: str
    short_reason: str
    key_evidence: list[str]
    source_urls: list[str]
    raw_json: dict[str, Any]


def format_probability_percent(value: float) -> str:
    return f"{round(clamp_probability(value) * 100)}%"


def clamp_probability(value: float) -> float:
    return max(MIN_PROBABILITY, min(MAX_PROBABILITY, value))


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def market_close_datetime(row: dict[str, str]) -> datetime | None:
    close_time = parse_float(row.get("closeTime"))
    if close_time is not None:
        timestamp = close_time / 1000 if close_time > 10_000_000_000 else close_time
        return datetime.fromtimestamp(timestamp, tz=ZoneInfo(TIMEZONE))

    close_date = (row.get("closeDate") or "").strip()
    if not close_date:
        return None
    try:
        parsed = datetime.fromisoformat(close_date.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(TIMEZONE))
    return parsed.astimezone(ZoneInfo(TIMEZONE))


def is_market_closed(row: dict[str, str], now: datetime) -> bool:
    close_time = market_close_datetime(row)
    return close_time is not None and now >= close_time


def market_close_prompt_value(row: dict[str, str]) -> str:
    close_time = market_close_datetime(row)
    if close_time is not None:
        return close_time.isoformat()
    return (row.get("closeDate") or "").strip()


def current_datetime_prompt_value() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).replace(microsecond=0).isoformat()


def format_probability(value: float) -> str:
    return f"{clamp_probability(value):.6f}"


def make_direct_reason(value: str, max_chars: int = 190) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if not text:
        return ""
    first_sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0].strip()
    text = first_sentence or text
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def clean_source_url(url: str) -> str:
    parts = urlsplit(url.strip())
    filtered_query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in {"fbclid", "gclid", "mc_cid", "mc_eid"}
    ]
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(filtered_query),
            "",
        )
    )


def normalize_chat_url(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    if base_url.endswith("/responses"):
        return base_url
    if base_url.endswith("/v1"):
        return f"{base_url}/responses"
    return f"{base_url}/v1/responses"


def fetch_json(
    url: str,
    params: dict[str, Any] | None = None,
    max_retries: int = 4,
) -> Any:
    query = urlencode({k: v for k, v in (params or {}).items() if v is not None})
    full_url = f"{url}?{query}" if query else url
    request = Request(
        full_url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
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


def fetch_market_description(market_id: str) -> str:
    detail = fetch_json(f"{MANIFOLD_API_BASE}/market/{market_id}")
    if not isinstance(detail, dict):
        return ""
    description = detail.get("textDescription")
    return description.strip() if isinstance(description, str) else ""


def load_rows(path: str) -> tuple[list[dict[str, str]], list[str]]:
    with open(path, newline="", encoding="utf-8") as input_file:
        reader = csv.DictReader(input_file)
        if reader.fieldnames is None:
            raise ValueError(f"No CSV header found in {path}")
        return list(reader), list(reader.fieldnames)


def write_rows(path: str, rows: list[dict[str, str]], input_fieldnames: list[str]) -> None:
    fieldnames = list(input_fieldnames)
    for column in OUTPUT_COLUMNS:
        if column not in fieldnames:
            fieldnames.append(column)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def safe_path_part(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip())
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "unknown"


def safe_timestamp(value: str) -> str:
    text = value.strip()
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed.strftime("%Y%m%dT%H%M%S%z")
        except ValueError:
            pass
    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y%m%dT%H%M%S%z")


def prediction_history_payload(row: dict[str, str]) -> dict[str, Any]:
    return {
        "id": row.get("id", ""),
        "question": row.get("question", ""),
        "marketUrl": row.get("url", ""),
        "status": row.get("forecastStatus", "forecast"),
        "forecastTimestamp": row.get("forecastTimestamp", ""),
        "forecastCurrentDate": row.get("forecastCurrentDate", ""),
        "forecastClosedAt": row.get("forecastClosedAt", ""),
        "model": row.get("forecastModel", ""),
        "inputPolicy": row.get("forecastInputPolicy", ""),
        "yesProbability": parse_float(
            row.get("finalPredictedYesProbability")
            or row.get("newsPredictedYesProbability")
        ),
        "noProbability": parse_float(
            row.get("finalPredictedNoProbability")
            or row.get("newsPredictedNoProbability")
        ),
        "confidence": row.get("newsConfidence", ""),
        "reason": row.get("finalShortReason") or row.get("newsShortReason", ""),
        "signals": {
            "search": parse_float(row.get("searchPredictedYesProbability")),
        },
        "weights": {
            "search": parse_float(row.get("ensembleSearchWeight")),
        },
        "evidence": [
            item.strip()
            for item in row.get("newsKeyEvidence", "").split("|")
            if item.strip()
        ],
        "sources": [
            item.strip()
            for item in row.get("newsSourceUrls", "").split("|")
            if item.strip()
        ],
        "searchQueries": [
            item.strip()
            for item in row.get("newsSearchQueries", "").split("|")
            if item.strip()
        ],
        "raw": row.get("newsRawJson", ""),
    }


def prediction_csv_fieldnames(rows: list[dict[str, str]]) -> list[str]:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    return fieldnames


def write_csv_file(
    path: str,
    rows: list[dict[str, str]],
    fieldnames: list[str],
) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def append_history_csv(
    path: str,
    row: dict[str, str],
    fieldnames: list[str],
) -> None:
    rows: list[dict[str, str]] = []
    merged_fieldnames = list(fieldnames)
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as input_file:
            reader = csv.DictReader(input_file)
            if reader.fieldnames:
                for key in reader.fieldnames:
                    if key not in merged_fieldnames:
                        merged_fieldnames.append(key)
            rows = list(reader)
    for key in row:
        if key not in merged_fieldnames:
            merged_fieldnames.append(key)
    rows.append(row)
    write_csv_file(path, rows, merged_fieldnames)


def write_prediction_history(history_dir: str, rows: list[dict[str, str]]) -> None:
    os.makedirs(history_dir, exist_ok=True)
    csv_fieldnames = prediction_csv_fieldnames(rows)
    run_timestamp = safe_timestamp(rows[0].get("forecastTimestamp", "")) if rows else ""
    if rows and csv_fieldnames:
        write_csv_file(os.path.join(history_dir, "latest.csv"), rows, csv_fieldnames)
        write_csv_file(
            os.path.join(history_dir, f"{run_timestamp}.csv"),
            rows,
            csv_fieldnames,
        )
        write_csv_file(
            os.path.join(history_dir, "runs", f"{run_timestamp}.csv"),
            rows,
            csv_fieldnames,
        )
    for row in rows:
        market_id = safe_path_part(row.get("id", ""))
        timestamp = safe_timestamp(row.get("forecastTimestamp", ""))
        market_dir = os.path.join(history_dir, market_id)
        os.makedirs(market_dir, exist_ok=True)
        write_csv_file(
            os.path.join(market_dir, f"{timestamp}.csv"),
            [row],
            csv_fieldnames,
        )
        write_csv_file(os.path.join(market_dir, "latest.csv"), [row], csv_fieldnames)
        append_history_csv(
            os.path.join(market_dir, "history.csv"),
            row,
            csv_fieldnames,
        )
        payload = prediction_history_payload(row)
        for filename in (f"{timestamp}.json", "latest.json"):
            path = os.path.join(market_dir, filename)
            with open(path, "w", encoding="utf-8", newline="\n") as output_file:
                json.dump(payload, output_file, indent=2, ensure_ascii=False)
                output_file.write("\n")


def clean_description(description: str, max_chars: int) -> str:
    description = re.sub(r"\s+", " ", description or "").strip()
    if len(description) <= max_chars:
        return description
    return description[: max_chars - 20].rstrip() + " [truncated]"


def build_prompt(
    question: str,
    description: str,
    current_date: str,
    close_date: str,
    blocked_domains: list[str],
) -> str:
    blocked_text = ", ".join(blocked_domains) if blocked_domains else "[none]"
    return f"""Forecast this binary prediction market using web-search evidence.

You must use web search to find recent, relevant, authoritative evidence. Search
for news and primary sources when the question depends on current events. Use
the resolution criteria in the description over the title if they differ.

You are only allowed to use the following market inputs:
- question
- description
- current_date, including hour and timezone
- market_closing_date

Do not assume any Manifold market probability, trading volume, creator identity,
comments, or bettor behavior.
If prediction-market odds or trading prices appear inside the description, do
not use them as evidence unless the market explicitly resolves based on those
odds.
Do not use prediction-market or forecasting-site pages as evidence. Blocked
forecasting domains: {blocked_text}.

Return a calibrated probability that the market ultimately resolves YES.
Make short_reason one direct sentence, 12-24 words, with the main evidence
first. Avoid filler, hedging phrases, citations, and markdown links in
short_reason. Put URLs only in source_urls.

Current date and time: {current_date}
Market closing date: {close_date or "[unknown]"}
Question: {question}
Description: {description or "[empty]"}
"""


def response_schema() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "name": "market_news_forecast",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "yes_probability": {
                    "type": "number",
                    "description": "Probability the market resolves YES, from 0 to 1.",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                },
                "short_reason": {
                    "type": "string",
                    "description": "One direct sentence, 12-24 words, explaining the main evidence for the forecast.",
                },
                "key_evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Short bullets summarizing the most important evidence.",
                },
                "source_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "URLs for the sources used, if available.",
                },
            },
            "required": [
                "yes_probability",
                "confidence",
                "short_reason",
                "key_evidence",
                "source_urls",
            ],
        },
    }


def call_openai_responses(
    prompt: str,
    model: str,
    web_tool: str,
    api_key: str,
    base_url: str,
    blocked_domains: list[str],
    use_domain_filters: bool,
) -> dict[str, Any]:
    url = normalize_chat_url(base_url)
    tool: dict[str, Any] = {"type": web_tool}
    if web_tool == "web_search":
        tool["search_context_size"] = "medium"
        if blocked_domains and use_domain_filters:
            tool["filters"] = {"blocked_domains": blocked_domains}

    body = {
        "model": model,
        "tools": [tool],
        "tool_choice": "auto",
        "include": ["web_search_call.action.sources"],
        "max_output_tokens": 6000,
        "input": [
            {
                "role": "system",
                "content": (
                    "You are a calibrated superforecaster. You search the web, "
                    "separate resolution criteria from background facts, avoid "
                    "overconfidence, write direct one-sentence rationales, and "
                    "always return valid JSON."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "text": {"format": response_schema()},
    }
    request = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=180) as response:
            return json.load(response)
    except HTTPError as error:
        try:
            body = error.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise RuntimeError(f"OpenAI HTTP {error.code}: {body[:1000]}") from error


def extract_output_text(response: dict[str, Any]) -> str:
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    pieces: list[str] = []
    for item in response.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                pieces.append(text)
    return "\n".join(pieces).strip()


def extract_search_queries(response: dict[str, Any]) -> list[str]:
    queries: list[str] = []
    for item in response.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "web_search_call":
            continue
        action = item.get("action")
        if not isinstance(action, dict):
            continue
        query = action.get("query")
        if isinstance(query, str) and query not in queries:
            queries.append(query)
    return queries


def extract_response_sources(response: dict[str, Any]) -> list[str]:
    urls: list[str] = []

    def add_url(value: Any) -> None:
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            if value not in urls:
                urls.append(value)

    for item in response.get("output", []):
        if not isinstance(item, dict):
            continue
        action = item.get("action")
        if isinstance(action, dict):
            for source in action.get("sources", []) or []:
                if isinstance(source, dict):
                    add_url(source.get("url"))
        for content in item.get("content", []) or []:
            if not isinstance(content, dict):
                continue
            for annotation in content.get("annotations", []) or []:
                if isinstance(annotation, dict):
                    add_url(annotation.get("url"))

    return urls


def parse_forecast(response: dict[str, Any], max_source_urls: int) -> Forecast:
    text = extract_output_text(response)
    if not text:
        raise ValueError("OpenAI response did not contain output text")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise
        payload = json.loads(match.group(0))

    probability = parse_float(payload.get("yes_probability"))
    if probability is None:
        raise ValueError(f"Forecast missing yes_probability: {text[:300]}")

    confidence = str(payload.get("confidence") or "low").strip().lower()
    if confidence not in {"low", "medium", "high"}:
        confidence = "low"

    key_evidence = payload.get("key_evidence")
    if not isinstance(key_evidence, list):
        key_evidence = []
    key_evidence = [str(item).strip() for item in key_evidence if str(item).strip()]

    source_urls = payload.get("source_urls")
    if not isinstance(source_urls, list):
        source_urls = []
    cleaned_urls: list[str] = []
    for url in source_urls:
        cleaned = clean_source_url(str(url))
        if cleaned and cleaned not in cleaned_urls:
            cleaned_urls.append(cleaned)
    source_urls = cleaned_urls
    for url in extract_response_sources(response):
        cleaned = clean_source_url(url)
        if cleaned and cleaned not in source_urls:
            source_urls.append(cleaned)
        if len(source_urls) >= max_source_urls:
            break
    source_urls = source_urls[:max_source_urls]

    return Forecast(
        yes_probability=clamp_probability(probability),
        confidence=confidence,
        short_reason=make_direct_reason(str(payload.get("short_reason") or "")),
        key_evidence=key_evidence,
        source_urls=source_urls,
        raw_json=payload,
    )


def forecast_market(
    row: dict[str, str],
    current_date: str,
    model: str,
    web_tool: str,
    api_key: str,
    base_url: str,
    description_max_chars: int,
    fetch_live_description: bool,
    blocked_domains: list[str],
    use_domain_filters: bool,
    max_source_urls: int,
) -> tuple[Forecast, list[str]]:
    description = row.get("textDescription", "").strip()
    if fetch_live_description and row.get("id"):
        live_description = fetch_market_description(row["id"])
        if live_description:
            description = live_description

    prompt = build_prompt(
        question=row.get("question", "").strip(),
        description=clean_description(description, description_max_chars),
        current_date=current_date,
        close_date=market_close_prompt_value(row),
        blocked_domains=blocked_domains,
    )
    response = call_openai_responses(
        prompt=prompt,
        model=model,
        web_tool=web_tool,
        api_key=api_key,
        base_url=base_url,
        blocked_domains=blocked_domains,
        use_domain_filters=use_domain_filters,
    )
    return parse_forecast(response, max_source_urls), extract_search_queries(response)


def make_search_reason(
    final_probability: float,
    search_forecast: Forecast,
) -> str:
    label = "YES" if final_probability >= 0.5 else "NO"
    evidence = search_forecast.short_reason
    reason = (
        f"Search evidence gives {format_probability_percent(final_probability)} "
        f"{label}; {evidence}"
    )
    return make_direct_reason(reason)


def predict_row(
    row: dict[str, str],
    current_date: str,
    now: datetime,
    model: str,
    web_tool: str,
    api_key: str,
    base_url: str,
    description_max_chars: int,
    fetch_live_description: bool,
    blocked_domains: list[str],
    use_domain_filters: bool,
    max_source_urls: int,
) -> dict[str, str] | None:
    if row.get("outcomeType", "").strip().upper() != "BINARY":
        return None

    output = dict(row)
    output["forecastTimestamp"] = now.replace(microsecond=0).isoformat()
    output["forecastCurrentDate"] = current_date
    output["forecastModel"] = model
    output["forecastInputPolicy"] = "web_search + market_closing_date"

    close_time = market_close_datetime(row)
    if is_market_closed(row, now):
        closed_at = close_time.isoformat() if close_time is not None else ""
        output["forecastStatus"] = "closed"
        output["forecastClosedAt"] = closed_at
        output["finalPredictedYesProbability"] = ""
        output["finalPredictedNoProbability"] = ""
        output["finalShortReason"] = "Market closed. No prediction generated."
        output["searchPredictedYesProbability"] = ""
        output["searchPredictedNoProbability"] = ""
        output["searchConfidence"] = ""
        output["searchShortReason"] = ""
        output["ensembleSearchWeight"] = ""
        output["ensembleMethod"] = "direct_web_search_forecast"
        output["newsPredictedYesProbability"] = ""
        output["newsPredictedNoProbability"] = ""
        output["newsConfidence"] = ""
        output["newsShortReason"] = "Market closed. No prediction generated."
        output["newsKeyEvidence"] = ""
        output["newsSourceUrls"] = ""
        output["newsSearchQueries"] = ""
        output["newsRawJson"] = json.dumps(
            {"status": "closed", "closed_at": closed_at},
            ensure_ascii=False,
        )
        return output

    search_forecast, queries = forecast_market(
        row=row,
        current_date=current_date,
        model=model,
        web_tool=web_tool,
        api_key=api_key,
        base_url=base_url,
        description_max_chars=description_max_chars,
        fetch_live_description=fetch_live_description,
        blocked_domains=blocked_domains,
        use_domain_filters=use_domain_filters,
        max_source_urls=max_source_urls,
    )
    final_probability = search_forecast.yes_probability
    final_reason = make_search_reason(
        final_probability=final_probability,
        search_forecast=search_forecast,
    )
    final_confidence = search_forecast.confidence

    output["forecastStatus"] = "forecast"
    output["forecastClosedAt"] = close_time.isoformat() if close_time else ""
    output["finalPredictedYesProbability"] = format_probability(final_probability)
    output["finalPredictedNoProbability"] = format_probability(1 - final_probability)
    output["finalShortReason"] = final_reason
    output["searchPredictedYesProbability"] = format_probability(
        search_forecast.yes_probability,
    )
    output["searchPredictedNoProbability"] = format_probability(
        1 - search_forecast.yes_probability,
    )
    output["searchConfidence"] = search_forecast.confidence
    output["searchShortReason"] = search_forecast.short_reason
    output["ensembleSearchWeight"] = "1.000000"
    output["ensembleMethod"] = "direct_web_search_forecast"
    output["newsPredictedYesProbability"] = format_probability(final_probability)
    output["newsPredictedNoProbability"] = format_probability(1 - final_probability)
    output["newsConfidence"] = final_confidence
    output["newsShortReason"] = final_reason
    output["newsKeyEvidence"] = " | ".join(search_forecast.key_evidence)
    output["newsSourceUrls"] = " | ".join(search_forecast.source_urls)
    output["newsSearchQueries"] = " | ".join(queries)
    output["newsRawJson"] = json.dumps(
        {
            "final": {
                "yes_probability": final_probability,
                "confidence": final_confidence,
                "short_reason": final_reason,
            },
            "signals": {
                "search": search_forecast.raw_json,
            },
            "weights": {"search": 1.0},
            "method": "direct_web_search_forecast",
        },
        ensure_ascii=False,
    )
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Use OpenAI Responses API web search to forecast binary Manifold "
            "markets from question, description, current date/time, and market closing date."
        )
    )
    parser.add_argument(
        "--input",
        default=DEFAULT_INPUT_CSV,
        help=f"Input CSV path. Default: {DEFAULT_INPUT_CSV}",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_CSV,
        help=f"Output CSV path. Default: {DEFAULT_OUTPUT_CSV}",
    )
    parser.add_argument(
        "--history-dir",
        default=DEFAULT_HISTORY_DIR,
        help=f"Directory for prediction CSV/JSON history. Default: {DEFAULT_HISTORY_DIR}",
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="Do not write prediction history CSV/JSON files.",
    )
    parser.add_argument(
        "--current-date",
        default=current_datetime_prompt_value(),
        help=(
            "Current date/time shown to the model. "
            "Default: current America/New_York timestamp."
        ),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("MARKET_NEWS_MODEL", DEFAULT_MODEL),
        help=f"OpenAI model. Default: {DEFAULT_MODEL}, overridable by MARKET_NEWS_MODEL.",
    )
    parser.add_argument(
        "--web-tool",
        default=os.environ.get("MARKET_NEWS_WEB_TOOL", DEFAULT_WEB_TOOL),
        help=f"Responses API web search tool type. Default: {DEFAULT_WEB_TOOL}.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL),
        help=f"OpenAI-compatible base URL. Default: {DEFAULT_OPENAI_BASE_URL}.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N eligible binary markets.",
    )
    parser.add_argument(
        "--market-id",
        action="append",
        default=None,
        help="Only process this market id. May be supplied multiple times.",
    )
    parser.add_argument(
        "--description-max-chars",
        type=int,
        default=6000,
        help="Maximum description characters sent to the model.",
    )
    parser.add_argument(
        "--max-source-urls",
        type=int,
        default=DEFAULT_MAX_SOURCE_URLS,
        help=f"Maximum source URLs stored per forecast. Default: {DEFAULT_MAX_SOURCE_URLS}.",
    )
    parser.add_argument(
        "--fetch-live-description",
        action="store_true",
        help=(
            "Refresh only the market description from Manifold before forecasting. "
            "No probability, volume, creator, comments, or bets are sent to the model."
        ),
    )
    parser.add_argument(
        "--blocked-domain",
        action="append",
        default=None,
        help=(
            "Domain blocked from web search. May be supplied multiple times. "
            f"Default: {', '.join(DEFAULT_BLOCKED_DOMAINS)}."
        ),
    )
    parser.add_argument(
        "--use-domain-filters",
        action="store_true",
        help=(
            "Pass blocked domains to the OpenAI web_search filters parameter. "
            "Some models reject filters; by default blocked domains are enforced by prompt only."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and print the first prompt without calling the API.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = os.environ.get("OPENAI_API_KEY")
    now = datetime.now(ZoneInfo(TIMEZONE))
    rows, fieldnames = load_rows(args.input)
    requested_ids = set(args.market_id or [])
    blocked_domains = args.blocked_domain
    if blocked_domains is None:
        blocked_domains = DEFAULT_BLOCKED_DOMAINS

    candidate_rows = [
        row
        for row in rows
        if row.get("outcomeType", "").strip().upper() == "BINARY"
        and (not requested_ids or row.get("id") in requested_ids)
    ]
    if args.limit is not None:
        candidate_rows = candidate_rows[: args.limit]

    if args.dry_run:
        if not candidate_rows:
            print("No eligible binary markets matched the inputs.")
            return 1
        row = candidate_rows[0]
        if is_market_closed(row, now):
            close_time = market_close_datetime(row)
            close_text = close_time.isoformat() if close_time else "unknown"
            print(f"Market is closed at {close_text}. No prediction would be generated.")
            return 0
        description = row.get("textDescription", "")
        search_prompt = build_prompt(
            row.get("question", ""),
            clean_description(description, args.description_max_chars),
            args.current_date,
            market_close_prompt_value(row),
            blocked_domains,
        )
        print("=== web-search prompt ===")
        print(search_prompt)
        return 0

    if not api_key:
        print(
            "OPENAI_API_KEY is required. Use --dry-run to inspect the prompt without calling the API.",
            file=sys.stderr,
        )
        return 2

    outputs: list[dict[str, str]] = []
    skipped = 0
    closed = 0
    for row in candidate_rows:
        try:
            output = predict_row(
                row=row,
                current_date=args.current_date,
                now=now,
                model=args.model,
                web_tool=args.web_tool,
                api_key=api_key,
                base_url=args.base_url,
                description_max_chars=args.description_max_chars,
                fetch_live_description=args.fetch_live_description,
                blocked_domains=blocked_domains,
                use_domain_filters=args.use_domain_filters,
                max_source_urls=args.max_source_urls,
            )
        except Exception as error:
            skipped += 1
            print(
                f"error market={row.get('id', '')}: {type(error).__name__}: {error}",
                file=sys.stderr,
                flush=True,
            )
            continue

        if output is None:
            skipped += 1
            continue

        outputs.append(output)
        if output.get("forecastStatus") == "closed":
            closed += 1
        forecasted = len(outputs) - closed
        if len(outputs) == 1 or len(outputs) % 10 == 0:
            print(
                f"forecasted={forecasted} closed={closed} skipped={skipped}",
                file=sys.stderr,
            )

    write_rows(args.output, outputs, fieldnames)
    if not args.no_history:
        write_prediction_history(args.history_dir, outputs)
    summary = {
        "inputCsv": args.input,
        "outputCsv": args.output,
        "historyDir": "" if args.no_history else args.history_dir,
        "currentDate": args.current_date,
        "model": args.model,
        "webTool": args.web_tool,
        "forecastedRows": len(outputs) - closed,
        "closedRows": closed,
        "outputRows": len(outputs),
        "skippedRows": skipped,
        "inputPolicy": "web_search + market_closing_date",
        "blockedDomains": blocked_domains,
        "signalWeights": {"search": 1.0},
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
