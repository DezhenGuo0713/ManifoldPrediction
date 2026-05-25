#!/usr/bin/env python3
"""Predict final YES/NO probabilities for binary Manifold markets.

The default mode is deliberately usable without API keys:

    python predict_market_probabilities.py

It reads Markets/MarketsRandomization.csv and writes
Markets/MarketProbabilityPredictions.csv.

Optional live enrichment:

    python predict_market_probabilities.py --fetch-live

Optional OpenAI-compatible LLM mode:

    $env:OPENAI_API_KEY = "..."
    $env:MARKET_LLM_MODEL = "gpt-4o-mini"
    python predict_market_probabilities.py --mode hybrid --fetch-live

The model follows the same task shape as manifold-llm-bot: question,
description, creator, comments, and current date produce a probability in
[0, 1]. It also emits the Terminator2-style fields current probability,
model estimate, edge, side, and reasoning.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
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
DEFAULT_INPUT_CSV = os.path.join(MARKETS_DIR, "MarketsRandomization.csv")
DEFAULT_FALLBACK_INPUT_CSV = os.path.join(
    MARKETS_DIR,
    "active_markets_until_end_june_2026_with_descriptions.csv",
)
DEFAULT_OUTPUT_CSV = os.path.join(MARKETS_DIR, "MarketProbabilityPredictions.csv")

API_BASE = "https://api.manifold.markets/v0"
TIMEZONE = "America/New_York"
USER_AGENT = "manifold-final-probability-predictor/1.0"
DEFAULT_EDGE_THRESHOLD = 0.05
MIN_PROBABILITY = 0.01
MAX_PROBABILITY = 0.99

APPENDED_COLUMNS = [
    "predictionTimestamp",
    "marketProbability",
    "predictedYesProbability",
    "predictedNoProbability",
    "edgeYes",
    "recommendedSide",
    "predictionConfidence",
    "predictionSource",
    "modelMarketWeight",
    "daysToClose",
    "recentBetCount",
    "recentBetVolume",
    "recentProbabilityTrendPp",
    "commentsUsed",
    "predictionReasoning",
    "llmReasoning",
]


@dataclass(frozen=True)
class BetSummary:
    count: int = 0
    volume: float = 0.0
    probability_start: float | None = None
    probability_end: float | None = None
    trend: float | None = None
    yes_amount: float = 0.0
    no_amount: float = 0.0


@dataclass(frozen=True)
class Prediction:
    yes_probability: float
    source: str
    confidence: str
    market_weight: float
    reasoning: str
    llm_reasoning: str = ""


def clamp_probability(value: float) -> float:
    return min(MAX_PROBABILITY, max(MIN_PROBABILITY, value))


def logit(probability: float) -> float:
    probability = clamp_probability(probability)
    return math.log(probability / (1 - probability))


def sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1 / (1 + z)
    z = math.exp(value)
    return z / (1 + z)


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


def parse_int(value: Any) -> int | None:
    number = parse_float(value)
    if number is None:
        return None
    return int(number)


def format_probability(value: float) -> str:
    return f"{clamp_probability(value):.6f}"


def fetch_json(
    path: str,
    params: dict[str, Any] | None = None,
    max_retries: int = 4,
) -> Any:
    query = urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{API_BASE}{path}"
    if query:
        url = f"{url}?{query}"

    request = Request(
        url,
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


def normalize_api_array(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    if isinstance(response, dict) and isinstance(response.get("value"), list):
        return [item for item in response["value"] if isinstance(item, dict)]
    return []


def fetch_market_detail(market_id: str) -> dict[str, Any]:
    response = fetch_json(f"/market/{market_id}")
    if not isinstance(response, dict):
        raise RuntimeError(f"Unexpected /market/{market_id} response")
    return response


def fetch_comments(market_id: str, limit: int) -> list[dict[str, Any]]:
    response = fetch_json("/comments", {"contractId": market_id, "limit": limit})
    return normalize_api_array(response)


def fetch_bets(market_id: str, limit: int) -> list[dict[str, Any]]:
    response = fetch_json("/bets", {"contractId": market_id, "limit": limit})
    return normalize_api_array(response)


def rich_text_to_plain_text(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(
            text for item in node if (text := rich_text_to_plain_text(item)).strip()
        )
    if not isinstance(node, dict):
        return ""

    pieces: list[str] = []
    text = node.get("text")
    if isinstance(text, str):
        pieces.append(text)

    attrs = node.get("attrs")
    if isinstance(attrs, dict):
        src = attrs.get("src")
        href = attrs.get("href")
        if isinstance(src, str):
            pieces.append(src)
        if isinstance(href, str):
            pieces.append(href)

    content = node.get("content")
    if content:
        nested = rich_text_to_plain_text(content)
        if nested:
            pieces.append(nested)

    return " ".join(piece.strip() for piece in pieces if piece.strip())


def comment_to_plain_text(comment: dict[str, Any]) -> str:
    author = comment.get("userUsername") or comment.get("userName") or "unknown"
    created_time = parse_int(comment.get("createdTime"))
    created = ""
    if created_time is not None:
        created = datetime.fromtimestamp(created_time / 1000, tz=ZoneInfo(TIMEZONE)).date()
    body = rich_text_to_plain_text(comment.get("content"))
    body = re.sub(r"\s+", " ", body).strip()
    if not body:
        return ""
    return f"{created} {author}: {body}".strip()


def summarize_comments(comments: list[dict[str, Any]], max_chars: int = 4000) -> str:
    texts = [text for comment in comments if (text := comment_to_plain_text(comment))]
    summary = "\n".join(texts)
    if len(summary) <= max_chars:
        return summary
    return summary[: max_chars - 20].rstrip() + "\n[truncated]"


def summarize_bets(bets: list[dict[str, Any]]) -> BetSummary:
    if not bets:
        return BetSummary()

    sorted_bets = sorted(
        bets,
        key=lambda bet: parse_int(bet.get("createdTime")) or 0,
    )
    volume = 0.0
    yes_amount = 0.0
    no_amount = 0.0

    for bet in sorted_bets:
        amount = abs(parse_float(bet.get("amount")) or 0.0)
        volume += amount
        outcome = str(bet.get("outcome") or "").upper()
        if outcome == "YES":
            yes_amount += amount
        elif outcome == "NO":
            no_amount += amount

    first = sorted_bets[0]
    last = sorted_bets[-1]
    probability_start = parse_float(first.get("probBefore"))
    if probability_start is None:
        probability_start = parse_float(first.get("probAfter"))
    probability_end = parse_float(last.get("probAfter"))
    trend = None
    if probability_start is not None and probability_end is not None:
        trend = probability_end - probability_start

    return BetSummary(
        count=len(sorted_bets),
        volume=volume,
        probability_start=probability_start,
        probability_end=probability_end,
        trend=trend,
        yes_amount=yes_amount,
        no_amount=no_amount,
    )


def load_rows(path: str) -> tuple[list[dict[str, str]], list[str]]:
    with open(path, newline="", encoding="utf-8") as input_file:
        reader = csv.DictReader(input_file)
        if reader.fieldnames is None:
            raise ValueError(f"No CSV header found in {path}")
        return list(reader), list(reader.fieldnames)


def write_rows(path: str, rows: list[dict[str, str]], input_fieldnames: list[str]) -> None:
    fieldnames = list(input_fieldnames)
    for column in APPENDED_COLUMNS:
        if column not in fieldnames:
            fieldnames.append(column)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def row_text_description(row: dict[str, str], detail: dict[str, Any] | None) -> str:
    if detail:
        detail_description = detail.get("textDescription")
        if isinstance(detail_description, str) and detail_description.strip():
            return detail_description.strip()
    return row.get("textDescription", "").strip()


def row_probability(row: dict[str, str], detail: dict[str, Any] | None) -> float | None:
    if detail:
        probability = parse_float(detail.get("probability"))
        if probability is not None:
            return probability
    return parse_float(row.get("probability"))


def row_close_time(row: dict[str, str], detail: dict[str, Any] | None) -> int | None:
    if detail:
        close_time = parse_int(detail.get("closeTime"))
        if close_time is not None:
            return close_time
    return parse_int(row.get("closeTime"))


def days_to_close(
    row: dict[str, str],
    detail: dict[str, Any] | None,
    now: datetime,
) -> float | None:
    close_time = row_close_time(row, detail)
    if close_time is None:
        return None
    close = datetime.fromtimestamp(close_time / 1000, tz=now.tzinfo)
    return (close - now).total_seconds() / (24 * 60 * 60)


def market_reliability_weight(
    row: dict[str, str],
    detail: dict[str, Any] | None,
    description: str,
    bet_summary: BetSummary,
    days_remaining: float | None,
) -> float:
    volume = parse_float((detail or {}).get("volume")) or parse_float(row.get("volume")) or 0.0
    volume_24h = (
        parse_float((detail or {}).get("volume24Hours"))
        or parse_float(row.get("volume24Hours"))
        or 0.0
    )
    bettors = (
        parse_float((detail or {}).get("uniqueBettorCount"))
        or parse_float(row.get("uniqueBettorCount"))
        or 0.0
    )

    score = 0.45
    score += min(0.25, 0.06 * math.log10(volume + 1))
    score += min(0.15, 0.04 * math.log10(bettors + 1))
    score += min(0.08, 0.03 * math.log10(volume_24h + 1))
    score += min(0.06, 0.02 * math.log10(bet_summary.count + 1))

    if description:
        score += 0.04
    if days_remaining is not None:
        if days_remaining <= 1:
            score += 0.06
        elif days_remaining <= 7:
            score += 0.04
        elif days_remaining <= 30:
            score += 0.02

    return min(0.95, max(0.55, score))


def heuristic_prediction(
    row: dict[str, str],
    detail: dict[str, Any] | None,
    bet_summary: BetSummary,
    now: datetime,
) -> Prediction:
    market_probability = row_probability(row, detail)
    if market_probability is None:
        raise ValueError(f"Missing probability for market {row.get('id', '')}")
    market_probability = clamp_probability(market_probability)

    description = row_text_description(row, detail)
    days_remaining = days_to_close(row, detail, now)
    market_weight = market_reliability_weight(
        row,
        detail,
        description,
        bet_summary,
        days_remaining,
    )

    probability_logit = market_weight * logit(market_probability)

    trend_pp = None
    if bet_summary.trend is not None:
        trend_pp = 100 * bet_summary.trend
        trend_weight = min(0.22, 0.04 + 0.025 * math.log10(bet_summary.count + 1))
        probability_logit += trend_weight * (
            logit(clamp_probability(market_probability))
            - logit(clamp_probability(bet_summary.probability_start or market_probability))
        )

    predicted = clamp_probability(sigmoid(probability_logit))

    if days_remaining is not None and days_remaining <= 1:
        predicted = clamp_probability(0.75 * market_probability + 0.25 * predicted)

    if market_weight >= 0.82:
        confidence = "high"
    elif market_weight >= 0.70:
        confidence = "medium"
    else:
        confidence = "low"

    trend_text = "no recent bet trend"
    if trend_pp is not None:
        trend_text = f"recent trend {trend_pp:+.1f}pp over {bet_summary.count} bets"

    days_text = "unknown close date"
    if days_remaining is not None:
        days_text = f"{days_remaining:.1f} days to close"

    reasoning = (
        f"Market probability {market_probability:.3f}; "
        f"liquidity/time weight {market_weight:.2f}; "
        f"{trend_text}; {days_text}."
    )

    return Prediction(
        yes_probability=predicted,
        source="heuristic",
        confidence=confidence,
        market_weight=market_weight,
        reasoning=reasoning,
    )


def normalize_chat_url(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    if base_url.endswith("/v1"):
        return f"{base_url}/chat/completions"
    return f"{base_url}/v1/chat/completions"


def llm_is_configured() -> bool:
    api_key = os.environ.get("MARKET_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("MARKET_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    return bool(api_key or (base_url and "localhost" in base_url))


def build_llm_prompt(
    row: dict[str, str],
    detail: dict[str, Any] | None,
    comments_text: str,
    bet_summary: BetSummary,
    now: datetime,
    heuristic: Prediction,
) -> str:
    market_probability = row_probability(row, detail)
    description = row_text_description(row, detail)
    days_remaining = days_to_close(row, detail, now)
    close_date = row.get("closeDate", "")
    if detail and detail.get("closeTime"):
        close_time = parse_int(detail.get("closeTime"))
        if close_time is not None:
            close_date = datetime.fromtimestamp(
                close_time / 1000,
                tz=now.tzinfo,
            ).isoformat()

    trend = "unknown"
    if bet_summary.trend is not None:
        trend = f"{100 * bet_summary.trend:+.1f} percentage points"

    return f"""Predict the final resolution probability for this binary Manifold market.

Return JSON only with exactly these keys:
{{"yes_probability": 0.0, "reasoning": "short reasoning"}}

Rules:
- yes_probability must be a number between 0 and 1.
- Predict the probability the market ultimately resolves YES, not the best trade size.
- Treat the market price as useful evidence, especially when liquidity and bettor count are high.
- Use the resolution criteria in the description over the question title when they differ.

Current date: {now.date().isoformat()}
Question: {row.get("question", "")}
Description: {description}
Creator username: {row.get("creatorUsername", "")}
Close date: {close_date}
Days to close: {days_remaining if days_remaining is not None else "unknown"}
Current Manifold YES probability: {market_probability}
Volume: {(detail or {}).get("volume", row.get("volume", ""))}
24h volume: {(detail or {}).get("volume24Hours", row.get("volume24Hours", ""))}
Unique bettor count: {(detail or {}).get("uniqueBettorCount", row.get("uniqueBettorCount", ""))}
Recent bet count sampled: {bet_summary.count}
Recent bet volume sampled: {bet_summary.volume:.2f}
Recent probability trend: {trend}
Heuristic estimate: {heuristic.yes_probability:.3f}
Comments:
{comments_text or "[none fetched]"}
"""


def parse_llm_json(text: str) -> tuple[float, str]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            number_match = re.search(r"(?<!\d)(?:0(?:\.\d+)?|1(?:\.0+)?)(?!\d)", cleaned)
            if not number_match:
                raise
            return clamp_probability(float(number_match.group(0))), cleaned[:500]
        payload = json.loads(match.group(0))

    probability = parse_float(
        payload.get("yes_probability")
        or payload.get("answer")
        or payload.get("probability")
    )
    if probability is None:
        raise ValueError(f"LLM response had no yes_probability: {cleaned[:300]}")
    reasoning = str(payload.get("reasoning") or "").strip()
    return clamp_probability(probability), reasoning


def call_llm(prompt: str) -> tuple[float, str]:
    base_url = os.environ.get("MARKET_LLM_BASE_URL") or os.environ.get(
        "OPENAI_BASE_URL",
        "https://api.openai.com",
    )
    api_key = os.environ.get("MARKET_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    model = os.environ.get("MARKET_LLM_MODEL") or os.environ.get(
        "OPENAI_MODEL",
        "gpt-4o-mini",
    )
    chat_url = normalize_chat_url(base_url)

    body = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 500,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a calibrated prediction-market forecaster. "
                    "Always return a JSON probability between 0 and 1."
                ),
            },
            {"role": "user", "content": prompt},
        ],
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request = Request(
        chat_url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=90) as response:
        result = json.load(response)

    content = result["choices"][0]["message"]["content"]
    return parse_llm_json(content)


def hybrid_prediction(heuristic: Prediction, llm_probability: float, llm_reasoning: str) -> Prediction:
    market_weight = heuristic.market_weight
    llm_weight = min(0.65, max(0.25, 0.75 - market_weight / 2))
    combined = sigmoid(
        (1 - llm_weight) * logit(heuristic.yes_probability)
        + llm_weight * logit(llm_probability)
    )
    reasoning = (
        f"Hybrid estimate blended heuristic {heuristic.yes_probability:.3f} "
        f"with LLM {llm_probability:.3f}; LLM weight {llm_weight:.2f}. "
        f"{heuristic.reasoning}"
    )
    return Prediction(
        yes_probability=clamp_probability(combined),
        source="hybrid",
        confidence=heuristic.confidence,
        market_weight=market_weight,
        reasoning=reasoning,
        llm_reasoning=llm_reasoning,
    )


def select_side(predicted_yes: float, market_probability: float, edge_threshold: float) -> str:
    edge = predicted_yes - market_probability
    if edge >= edge_threshold:
        return "YES"
    if edge <= -edge_threshold:
        return "NO"
    return "HOLD"


def enrich_live(
    row: dict[str, str],
    fetch_live: bool,
    comments_limit: int,
    bets_limit: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]], str]:
    if not fetch_live:
        return None, [], [], ""

    market_id = row["id"]
    warnings: list[str] = []
    detail = None
    comments: list[dict[str, Any]] = []
    bets: list[dict[str, Any]] = []

    try:
        detail = fetch_market_detail(market_id)
    except Exception as error:
        warnings.append(f"detail={type(error).__name__}: {error}")

    try:
        comments = fetch_comments(market_id, comments_limit)
    except Exception as error:
        warnings.append(f"comments={type(error).__name__}: {error}")

    try:
        bets = fetch_bets(market_id, bets_limit)
    except Exception as error:
        warnings.append(f"bets={type(error).__name__}: {error}")

    return detail, comments, bets, "; ".join(warnings)


def predict_row(
    row: dict[str, str],
    mode: str,
    fetch_live: bool,
    comments_limit: int,
    bets_limit: int,
    edge_threshold: float,
    now: datetime,
) -> dict[str, str] | None:
    if row.get("outcomeType", "").strip().upper() != "BINARY":
        return None

    detail, comments, bets, live_warning = enrich_live(
        row,
        fetch_live,
        comments_limit,
        bets_limit,
    )
    market_probability = row_probability(row, detail)
    if market_probability is None:
        return None

    bet_summary = summarize_bets(bets)
    comments_text = summarize_comments(comments)
    prediction = heuristic_prediction(row, detail, bet_summary, now)

    if mode in {"llm", "hybrid"}:
        if not llm_is_configured():
            if mode == "llm":
                raise RuntimeError(
                    "LLM mode requires MARKET_LLM_API_KEY or OPENAI_API_KEY. "
                    "Use --mode heuristic to run without an LLM."
                )
            prediction = Prediction(
                yes_probability=prediction.yes_probability,
                source="heuristic-fallback",
                confidence=prediction.confidence,
                market_weight=prediction.market_weight,
                reasoning=(
                    prediction.reasoning
                    + " LLM was not configured, so hybrid mode used heuristic fallback."
                ),
            )
        else:
            prompt = build_llm_prompt(
                row,
                detail,
                comments_text,
                bet_summary,
                now,
                prediction,
            )
            llm_probability, llm_reasoning = call_llm(prompt)
            if mode == "llm":
                prediction = Prediction(
                    yes_probability=llm_probability,
                    source="llm",
                    confidence=prediction.confidence,
                    market_weight=prediction.market_weight,
                    reasoning=(
                        f"LLM estimate {llm_probability:.3f}. "
                        f"Heuristic reference: {prediction.reasoning}"
                    ),
                    llm_reasoning=llm_reasoning,
                )
            else:
                prediction = hybrid_prediction(prediction, llm_probability, llm_reasoning)

    predicted_yes = clamp_probability(prediction.yes_probability)
    market_probability = clamp_probability(market_probability)
    predicted_no = 1 - predicted_yes
    edge_yes = predicted_yes - market_probability
    days_remaining = days_to_close(row, detail, now)

    output = dict(row)
    output["predictionTimestamp"] = now.isoformat()
    output["marketProbability"] = format_probability(market_probability)
    output["predictedYesProbability"] = format_probability(predicted_yes)
    output["predictedNoProbability"] = format_probability(predicted_no)
    output["edgeYes"] = f"{edge_yes:.6f}"
    output["recommendedSide"] = select_side(
        predicted_yes,
        market_probability,
        edge_threshold,
    )
    output["predictionConfidence"] = prediction.confidence
    output["predictionSource"] = prediction.source
    output["modelMarketWeight"] = f"{prediction.market_weight:.3f}"
    output["daysToClose"] = "" if days_remaining is None else f"{days_remaining:.3f}"
    output["recentBetCount"] = str(bet_summary.count)
    output["recentBetVolume"] = f"{bet_summary.volume:.3f}"
    output["recentProbabilityTrendPp"] = (
        "" if bet_summary.trend is None else f"{100 * bet_summary.trend:.3f}"
    )
    output["commentsUsed"] = str(len(comments))
    reasoning = prediction.reasoning
    if live_warning:
        reasoning = f"{reasoning} Live fetch warning: {live_warning}"
    output["predictionReasoning"] = reasoning
    output["llmReasoning"] = prediction.llm_reasoning

    return output


def resolve_default_input(path: str) -> str:
    if path != DEFAULT_INPUT_CSV:
        return path
    if os.path.exists(DEFAULT_INPUT_CSV):
        return DEFAULT_INPUT_CSV
    return DEFAULT_FALLBACK_INPUT_CSV


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Predict final YES/NO probabilities for binary Manifold markets "
            "from the project CSV, optional live Manifold data, and optional LLM."
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
        "--mode",
        choices=["heuristic", "llm", "hybrid"],
        default="heuristic",
        help="Prediction mode. Default: heuristic.",
    )
    parser.add_argument(
        "--fetch-live",
        action="store_true",
        help="Fetch live market details, comments, and recent bets from Manifold.",
    )
    parser.add_argument(
        "--comments-limit",
        type=int,
        default=12,
        help="Maximum comments to fetch per market in --fetch-live mode.",
    )
    parser.add_argument(
        "--bets-limit",
        type=int,
        default=100,
        help="Maximum recent bets to fetch per market in --fetch-live mode.",
    )
    parser.add_argument(
        "--edge-threshold",
        type=float,
        default=DEFAULT_EDGE_THRESHOLD,
        help="YES/NO recommendation threshold in probability points. Default: 0.05.",
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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_csv = resolve_default_input(args.input)
    rows, fieldnames = load_rows(input_csv)
    now = datetime.now(ZoneInfo(TIMEZONE))
    requested_ids = set(args.market_id or [])

    outputs: list[dict[str, str]] = []
    skipped = 0
    for row in rows:
        if requested_ids and row.get("id") not in requested_ids:
            continue
        if args.limit is not None and len(outputs) >= args.limit:
            break

        try:
            prediction = predict_row(
                row,
                args.mode,
                args.fetch_live,
                args.comments_limit,
                args.bets_limit,
                args.edge_threshold,
                now,
            )
        except Exception as error:
            print(
                f"error market={row.get('id', '')}: {type(error).__name__}: {error}",
                file=sys.stderr,
                flush=True,
            )
            skipped += 1
            continue

        if prediction is None:
            skipped += 1
            continue
        outputs.append(prediction)

        if len(outputs) == 1 or len(outputs) % 25 == 0:
            print(f"predicted={len(outputs)} skipped={skipped}", file=sys.stderr)

    write_rows(args.output, outputs, fieldnames)

    side_counts: dict[str, int] = {}
    for row in outputs:
        side = row["recommendedSide"]
        side_counts[side] = side_counts.get(side, 0) + 1

    summary = {
        "inputCsv": input_csv,
        "outputCsv": args.output,
        "mode": args.mode,
        "fetchLive": args.fetch_live,
        "eligiblePredictedRows": len(outputs),
        "skippedRows": skipped,
        "sideCounts": side_counts,
        "edgeThreshold": args.edge_threshold,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
