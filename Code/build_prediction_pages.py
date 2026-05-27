#!/usr/bin/env python3
"""Build a static GitHub Pages site for market news forecasts.

Default behavior:

    python Code/build_prediction_pages.py

The builder prefers Markets/MarketNewsPredictions.csv when present, otherwise
falls back to the 10-market sample created during development. Output is written
to docs/, which can be selected directly as a GitHub Pages source.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import shutil
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKETS_DIR = os.path.join(PROJECT_DIR, "Markets")
DOCS_DIR = os.path.join(PROJECT_DIR, "docs")
DISPLAY_TIMEZONE = ZoneInfo("America/New_York")
PUBLIC_SITE_BASE_URL = os.environ.get(
    "PREDICTION_SITE_BASE_URL",
    "https://dezhenguo0713.github.io/ManifoldPrediction",
).rstrip("/")
PUBLIC_CARD_IMAGE_BASE_URL = os.environ.get(
    "PREDICTION_CARD_IMAGE_BASE_URL",
    "https://raw.githubusercontent.com/DezhenGuo0713/ManifoldPrediction/main/docs/cards",
).rstrip("/")
PUBLIC_LINK_CARD_IMAGE_BASE_URL = os.environ.get(
    "PREDICTION_LINK_CARD_IMAGE_BASE_URL",
    "https://raw.githubusercontent.com/DezhenGuo0713/ManifoldPrediction/main/docs/link-cards",
).rstrip("/")

PREFERRED_INPUTS = [
    os.path.join(MARKETS_DIR, "MarketNewsPredictions.csv"),
    os.path.join(MARKETS_DIR, "MarketNewsPredictions.10_sample.csv"),
    os.path.join(MARKETS_DIR, "MarketNewsPredictions.sample.csv"),
]


def h(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def parse_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pct(value: Any) -> str:
    return f"{round(parse_float(value) * 100)}%"


def truncate_text(value: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def format_edt_timestamp(value: str, fallback: str = "") -> str:
    text = (value or "").strip()
    if not text:
        return fallback
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=DISPLAY_TIMEZONE)
    return parsed.astimezone(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")


def split_pipe(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split("|") if part.strip()]


def clean_evidence_text(value: str) -> str:
    value = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1", value)
    value = re.sub(r"\(\s*([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\s*\)", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def source_label(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host or url


def market_close_datetime(row: dict[str, str]) -> datetime | None:
    close_time = parse_optional_float(row.get("closeTime"))
    if close_time is not None:
        timestamp = close_time / 1000 if close_time > 10_000_000_000 else close_time
        return datetime.fromtimestamp(timestamp, tz=DISPLAY_TIMEZONE)

    close_date = (row.get("closeDate") or row.get("forecastClosedAt") or "").strip()
    if not close_date:
        return None
    try:
        parsed = datetime.fromisoformat(close_date.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=DISPLAY_TIMEZONE)
    return parsed.astimezone(DISPLAY_TIMEZONE)


def market_close_label(row: dict[str, str]) -> str:
    close_time = market_close_datetime(row)
    if close_time is not None:
        return close_time.strftime("%Y-%m-%d %H:%M:%S %Z")
    return "Market closed"


def is_market_closed(row: dict[str, str]) -> bool:
    if (row.get("forecastStatus") or "").strip().lower() == "closed":
        return True
    close_time = market_close_datetime(row)
    return close_time is not None and datetime.now(DISPLAY_TIMEZONE) >= close_time


def is_displayable_row(row: dict[str, str]) -> bool:
    return bool(row.get("id")) and (
        bool(row.get("newsPredictedYesProbability")) or is_market_closed(row)
    )


def market_symbol(question: str) -> str:
    text = question.lower()
    if "bitcoin" in text or "btc" in text:
        return "BTC"
    if "apple" in text or "vision pro" in text or "wwdc" in text:
        return "A"
    if "spacex" in text or "starship" in text:
        return "X"
    if "fifa" in text or "world cup" in text:
        return "FIFA"
    if "s&p" in text or "dollar" in text or "$" in text:
        return "$"
    if "ai" in text or "openai" in text:
        return "AI"
    return "?"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "market"


def resolve_input(path: str | None) -> str:
    if path:
        return path
    for candidate in PREFERRED_INPUTS:
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError(
        "No prediction CSV found. Run predict_market_news.py first."
    )


def load_rows(path: str) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as input_file:
        reader = csv.DictReader(input_file)
        return [row for row in reader if is_displayable_row(row)]


def write_text(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as output:
        output.write(content)


def probability_band(yes_probability: float) -> str:
    if yes_probability >= 0.65:
        return "yes-lean"
    if yes_probability <= 0.35:
        return "no-lean"
    return "uncertain"


def evidence_html(row: dict[str, str]) -> str:
    items = split_pipe(row.get("newsKeyEvidence", ""))
    if not items:
        return ""
    evidence = "\n".join(
        f"<li>{h(clean_evidence_text(item))}</li>" for item in items[:5]
    )
    return f"""
      <section class="evidence" aria-label="Key evidence">
        <h2>Evidence</h2>
        <ul>{evidence}</ul>
      </section>
"""


def source_html(row: dict[str, str]) -> str:
    urls = split_pipe(row.get("newsSourceUrls", ""))
    if not urls:
        return '<p class="empty">No source URLs were returned.</p>'
    links = []
    for url in urls[:8]:
        links.append(
            f'<a href="{h(url)}" target="_blank" rel="noopener noreferrer">'
            f"{h(source_label(url))}</a>"
        )
    return "\n".join(links)


def compact_source_html(row: dict[str, str]) -> str:
    urls = split_pipe(row.get("newsSourceUrls", ""))
    if not urls:
        return '<span class="source-pill">No sources</span>'
    links = []
    for index, url in enumerate(urls[:2], start=1):
        links.append(
            f'<a class="source-pill" href="{h(url)}" target="_blank" '
            f'rel="noopener noreferrer" title="{h(url)}">'
            f"Source {index}: {h(source_label(url))}</a>"
        )
    return "\n".join(links)


def market_card(row: dict[str, str], site_root: str) -> str:
    is_closed = is_market_closed(row)
    yes = parse_float(row.get("newsPredictedYesProbability"))
    no = parse_float(row.get("newsPredictedNoProbability"), 1 - yes)
    band = "closed" if is_closed else probability_band(yes)
    yes_score_class = "higher-score" if yes >= no else "lower-score"
    no_score_class = "higher-score" if no > yes else "lower-score"
    reason = (
        "Market closed. No prediction generated."
        if is_closed
        else row.get("newsShortReason", "").strip()
    )
    question = row.get("question", "").strip()
    market_url = row.get("url", "").strip()
    forecast_date = (
        f"Closed: {market_close_label(row)}"
        if is_closed
        else format_edt_timestamp(
            row.get("forecastTimestamp", ""),
            row.get("forecastCurrentDate", "").strip(),
        )
    )
    model = row.get("forecastModel", "").strip()
    confidence = "closed" if is_closed else row.get("newsConfidence", "").strip() or "unknown"
    source_count = min(2, len(split_pipe(row.get("newsSourceUrls", ""))))
    symbol = market_symbol(question)
    odds_html = (
        """
      <section class="poster-closed" aria-label="Market status">
        <span>MARKET CLOSED</span>
        <strong>No prediction</strong>
      </section>"""
        if is_closed
        else f"""
      <section class="poster-odds" aria-label="Forecast probabilities">
        <div class="odds-model">model: {h(model or "unknown")}</div>
        <div class="odds-side yes-side">
          <span class="outcome-label {yes_score_class}">YES</span>
          <strong class="{yes_score_class}">{h(pct(yes))}</strong>
        </div>
        <div class="split-line" aria-hidden="true"></div>
        <div class="odds-side no-side">
          <span class="outcome-label {no_score_class}">NO</span>
          <strong class="{no_score_class}">{h(pct(no))}</strong>
        </div>
      </section>"""
    )
    sources_html = (
        ""
        if is_closed
        else f"""
          <div class="poster-sources" aria-label="Sources">
            <span class="source-caption">Source</span>
            {compact_source_html(row)}
          </div>"""
    )
    manifold_link = (
        f'<a class="lock-button" href="{h(market_url)}" target="_blank" '
        f'rel="noopener noreferrer" aria-label="Open Manifold market">'
        f'<span class="lock-icon" aria-hidden="true"></span></a>'
        if market_url
        else ""
    )

    return f"""
    <article class="forecast-poster {band}">
      <div class="poster-glow" aria-hidden="true"></div>
      <div class="poster-watermark" aria-hidden="true">{h(symbol)}</div>

      <header class="poster-header">
        <h1>{h(question)}</h1>
        <div class="poster-meta">
          <span>Conf: {h(confidence.upper())}</span>
          <span>Src: {source_count}</span>
          <span>{h(model)}</span>
        </div>
      </header>

{odds_html}

      <footer class="poster-footer">
        <div class="footer-copy">
          <span class="poster-date">{h(forecast_date)}</span>
          <p class="poster-reason"><span>Reason:</span> {h(reason)}</p>
{sources_html}
        </div>
        {manifold_link}
      </footer>
    </article>
"""


def page_template(
    title: str,
    description: str,
    body: str,
    site_root: str,
    extra_head: str = "",
    og_title: str | None = None,
    og_description: str | None = None,
) -> str:
    preview_title = og_title or title
    preview_description = og_description or description
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{h(title)}</title>
  <meta name="description" content="{h(description)}">
  <meta property="og:title" content="{h(preview_title)}">
  <meta property="og:description" content="{h(preview_description)}">
  <meta property="og:type" content="website">
  <link rel="stylesheet" href="{site_root}assets/styles.css">
  {extra_head}
</head>
<body>
{body}
</body>
</html>
"""


def build_market_page(row: dict[str, str], output_dir: str) -> str:
    market_id = slugify(row["id"])
    page_dir = os.path.join(output_dir, "markets", market_id)
    relative_url = f"markets/{market_id}/"
    is_closed = is_market_closed(row)
    if is_closed:
        yes = "Closed"
        no = "No prediction"
        title = f"Market closed - {row.get('question', '')}"
        description = "Market closed. No prediction generated."
    else:
        yes = pct(row.get("newsPredictedYesProbability"))
        no = pct(row.get("newsPredictedNoProbability"))
        title = f"{yes} YES / {no} NO - {row.get('question', '')}"
        description = row.get("newsShortReason", "")
    absolute_page_url = f"{PUBLIC_SITE_BASE_URL}/{relative_url}"
    absolute_image_url = f"{PUBLIC_LINK_CARD_IMAGE_BASE_URL}/{market_id}.png"
    forecast_date = (
        f"closed {market_close_label(row)}"
        if is_closed
        else format_edt_timestamp(
            row.get("forecastTimestamp", ""),
            row.get("forecastCurrentDate", "").strip(),
        )
    )
    preview_title = (
        "AIBot forecast: Market closed"
        if is_closed
        else f"AIBot forecast: {yes} YES / {no} NO"
    )
    preview_description = truncate_text(
        f"{row.get('question', '')} Updated {forecast_date}. {description}",
        240,
    )
    extra_head = f"""
  <link rel="canonical" href="{h(absolute_page_url)}">
  <meta property="og:url" content="{h(absolute_page_url)}">
  <meta property="og:image" content="{h(absolute_image_url)}">
  <meta property="og:image:secure_url" content="{h(absolute_image_url)}">
  <meta property="og:image:type" content="image/png">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="600">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{h(preview_title)}">
  <meta name="twitter:description" content="{h(preview_description)}">
  <meta name="twitter:image" content="{h(absolute_image_url)}">
"""
    body = f"""
  <main class="embed-shell">
{market_card(row, "../../")}
  </main>
"""
    write_text(
        os.path.join(page_dir, "index.html"),
        page_template(
            title,
            description,
            body,
            "../../",
            extra_head=extra_head,
            og_title=preview_title,
            og_description=preview_description,
        ),
    )
    return relative_url


def build_link_preview_page(row: dict[str, str], output_dir: str) -> None:
    market_id = slugify(row["id"])
    page_dir = os.path.join(output_dir, "link-previews", market_id)
    yes = pct(row.get("newsPredictedYesProbability"))
    no = pct(row.get("newsPredictedNoProbability"))
    title = f"Link preview image - {yes} YES / {no} NO"
    description = row.get("newsShortReason", "")
    body = f"""
  <main class="embed-shell preview-shell">
{market_card(row, "../../")}
  </main>
"""
    write_text(
        os.path.join(page_dir, "index.html"),
        page_template(title, description, body, "../../"),
    )


def build_index(rows: list[dict[str, str]], page_urls: dict[str, str], output_dir: str) -> None:
    cards = []
    for row in rows:
        url = page_urls[row["id"]]
        yes = parse_float(row.get("newsPredictedYesProbability"))
        if is_market_closed(row):
            row_band = "closed"
            probs = '<strong>Market closed</strong><span>No prediction</span>'
        else:
            row_band = probability_band(yes)
            probs = (
                f"<strong>{h(pct(row.get('newsPredictedYesProbability')))} YES</strong>"
                f"<span>{h(pct(row.get('newsPredictedNoProbability')))} NO</span>"
            )
        cards.append(
            f"""
        <a class="market-row {row_band}" href="{h(url)}">
          <span class="row-question">{h(row.get("question", ""))}</span>
          <span class="row-probs">
            {probs}
          </span>
        </a>"""
        )

    generated = datetime.now(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")
    body = f"""
  <main class="site-shell">
    <header class="site-header">
      <p class="eyebrow">Market News Forecasts</p>
      <h1>Manifold Prediction Cards</h1>
      <p class="lead">Each market has a compact forecast card page with YES/NO probabilities, a short reason, and source links.</p>
      <p class="build-meta">Generated {h(generated)} from news-search forecasts.</p>
    </header>
    <section class="market-list" aria-label="Markets">
      {''.join(cards)}
    </section>
  </main>
"""
    write_text(
        os.path.join(output_dir, "index.html"),
        page_template(
            "Manifold Prediction Cards",
            "YES/NO probability cards generated from market news forecasts.",
            body,
            "",
        ),
    )


def build_json(rows: list[dict[str, str]], page_urls: dict[str, str], output_dir: str) -> None:
    payload = []
    for row in rows:
        payload.append(
            {
                "id": row.get("id", ""),
                "question": row.get("question", ""),
                "marketUrl": row.get("url", ""),
                "pageUrl": page_urls.get(row.get("id", ""), ""),
                "cardImageUrl": f"cards/{slugify(row.get('id', ''))}.png",
                "linkPreviewImageUrl": (
                    f"link-cards/{slugify(row.get('id', ''))}.png"
                ),
                "absolutePageUrl": (
                    f"{PUBLIC_SITE_BASE_URL}/"
                    f"{page_urls.get(row.get('id', ''), '')}"
                ),
                "absoluteCardImageUrl": (
                    f"{PUBLIC_CARD_IMAGE_BASE_URL}/"
                    f"{slugify(row.get('id', ''))}.png"
                ),
                "absoluteLinkPreviewImageUrl": (
                    f"{PUBLIC_LINK_CARD_IMAGE_BASE_URL}/"
                    f"{slugify(row.get('id', ''))}.png"
                ),
                "status": "closed" if is_market_closed(row) else "forecast",
                "yesProbability": (
                    None
                    if is_market_closed(row)
                    else parse_float(row.get("newsPredictedYesProbability"))
                ),
                "noProbability": (
                    None
                    if is_market_closed(row)
                    else parse_float(row.get("newsPredictedNoProbability"))
                ),
                "confidence": row.get("newsConfidence", ""),
                "reason": (
                    "Market closed. No prediction generated."
                    if is_market_closed(row)
                    else row.get("newsShortReason", "")
                ),
                "evidence": [] if is_market_closed(row) else split_pipe(row.get("newsKeyEvidence", "")),
                "sources": [] if is_market_closed(row) else split_pipe(row.get("newsSourceUrls", "")),
                "forecastDate": row.get("forecastCurrentDate", ""),
                "forecastTimestamp": format_edt_timestamp(
                    row.get("forecastTimestamp", ""),
                    row.get("forecastCurrentDate", ""),
                ),
                "closedAt": market_close_label(row) if is_market_closed(row) else "",
                "model": row.get("forecastModel", ""),
            }
        )
    write_text(
        os.path.join(output_dir, "predictions.json"),
        json.dumps(payload, indent=2, ensure_ascii=False),
    )


def build_css(output_dir: str) -> None:
    css = r""":root {
  color-scheme: dark;
  --bg: #050406;
  --panel: #120911;
  --ink: #f7f7f2;
  --muted: #b8adae;
  --dim: #6d6266;
  --line: rgba(255, 255, 255, 0.16);
  --yes: #00f04f;
  --no: #f4f0e8;
  --danger: #ff3b30;
  --amber: #f3a51d;
  --link: #f4f0e8;
  font-family: "Courier New", Courier, ui-monospace, monospace;
}

* {
  box-sizing: border-box;
}

html,
body {
  min-height: 100%;
}

body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
}

a {
  color: var(--link);
}

.site-shell {
  width: min(1120px, calc(100vw - 32px));
  margin: 0 auto;
  padding: 38px 0;
}

.site-header {
  margin-bottom: 26px;
}

.eyebrow,
.build-meta {
  color: var(--muted);
  font-size: 14px;
  margin: 0 0 8px;
}

.site-header h1 {
  margin: 0 0 12px;
  font-size: 42px;
  line-height: 1.05;
  letter-spacing: 0;
}

.lead {
  max-width: 760px;
  color: var(--muted);
  font-size: 16px;
  line-height: 1.45;
  margin: 0 0 10px;
}

.market-list {
  display: grid;
  gap: 12px;
}

.market-row {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 18px;
  align-items: center;
  min-height: 70px;
  padding: 16px 18px;
  background: #130a12;
  border: 1px solid var(--line);
  border-left: 6px solid var(--line);
  border-radius: 8px;
  color: inherit;
  text-decoration: none;
}

.market-row.yes-lean {
  border-left-color: var(--yes);
}

.market-row.no-lean {
  border-left-color: var(--danger);
}

.market-row.uncertain {
  border-left-color: var(--amber);
}

.market-row.closed {
  border-left-color: var(--danger);
}

.row-question {
  font-size: 16px;
  line-height: 1.35;
}

.row-probs {
  display: flex;
  gap: 12px;
  align-items: baseline;
  color: var(--muted);
  white-space: nowrap;
}

.row-probs strong {
  color: var(--yes);
}

.embed-shell {
  width: min(100vw, 1600px);
  margin: 0 auto;
  padding: 0;
}

.forecast-poster {
  position: relative;
  width: 100%;
  aspect-ratio: 16 / 9;
  min-height: 540px;
  overflow: hidden;
  background: #090407;
  border: 1px solid rgba(255, 255, 255, 0.08);
  color: var(--ink);
  isolation: isolate;
}

.forecast-poster.closed {
  border-color: rgba(255, 59, 48, 0.34);
}

.forecast-poster::before {
  content: "";
  position: absolute;
  inset: 0;
  background: #090407;
  box-shadow: inset 0 0 180px rgba(111, 45, 8, 0.22), inset 0 0 0 9999px rgba(0, 0, 0, 0.18);
  z-index: -3;
}

.poster-glow {
  display: none;
}

.poster-watermark {
  position: absolute;
  left: 50%;
  top: 49%;
  transform: translate(-50%, -50%) rotate(10deg);
  color: rgba(255, 255, 255, 0.13);
  font-size: min(31vw, 430px);
  font-weight: 900;
  line-height: 1;
  letter-spacing: 0;
  z-index: -1;
  user-select: none;
}

.poster-header {
  position: absolute;
  left: 4.5%;
  top: 3.5%;
  width: min(78%, 1080px);
}

.poster-header h1 {
  display: -webkit-box;
  margin: 0;
  overflow: hidden;
  color: var(--ink);
  font-size: min(3vw, 38px);
  font-weight: 800;
  line-height: 1.22;
  letter-spacing: 0;
  text-shadow: 0 3px 0 rgba(0, 0, 0, 0.65);
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.poster-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 34px;
  margin-top: 22px;
  color: var(--muted);
  font-size: min(2.2vw, 36px);
  line-height: 1.15;
  text-shadow: 0 2px 0 rgba(0, 0, 0, 0.8);
}

.poster-odds {
  position: absolute;
  left: 21%;
  right: 21%;
  top: 40%;
  display: grid;
  grid-template-columns: minmax(180px, 1fr) 1px minmax(180px, 1fr);
  align-items: center;
  column-gap: 6%;
  row-gap: 24px;
}

.odds-model {
  grid-column: 1 / -1;
  min-width: 0;
  overflow: hidden;
  color: var(--muted);
  font-size: min(1.9vw, 28px);
  font-weight: 700;
  line-height: 1;
  text-align: center;
  text-overflow: ellipsis;
  text-shadow: 0 2px 0 rgba(0, 0, 0, 0.78);
  white-space: nowrap;
}

.poster-closed {
  position: absolute;
  left: 16%;
  right: 16%;
  top: 40%;
  display: grid;
  gap: 24px;
  text-align: center;
}

.poster-closed span {
  color: var(--danger);
  font-size: min(5vw, 86px);
  font-weight: 900;
  line-height: 1;
  text-shadow: 0 5px 0 rgba(0, 0, 0, 0.75);
}

.poster-closed strong {
  color: var(--ink);
  font-size: min(3vw, 44px);
  line-height: 1;
  text-shadow: 0 3px 0 rgba(0, 0, 0, 0.78);
}

.split-line {
  width: 1px;
  height: 190px;
  background: rgba(255, 255, 255, 0.28);
}

.odds-side {
  display: grid;
  gap: 34px;
}

.no-side {
  text-align: right;
}

.outcome-label {
  color: var(--ink);
  font-size: min(3.1vw, 58px);
  font-weight: 700;
  line-height: 1;
  text-shadow: 0 3px 0 rgba(0, 0, 0, 0.8);
}

.odds-side strong {
  font-size: min(7.2vw, 130px);
  font-weight: 900;
  line-height: 0.95;
  letter-spacing: 0;
  text-shadow: 0 6px 0 rgba(0, 0, 0, 0.78);
}

.odds-side strong.higher-score {
  color: var(--yes);
}

.odds-side strong.lower-score {
  color: var(--no);
}

.outcome-label.higher-score {
  color: var(--yes);
}

.outcome-label.lower-score {
  color: var(--no);
}

.poster-footer {
  position: absolute;
  left: 4.5%;
  right: 4.5%;
  bottom: 4.4%;
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 30px;
  align-items: end;
}

.footer-copy {
  min-width: 0;
}

.poster-date {
  display: block;
  color: var(--ink);
  font-size: min(2.4vw, 44px);
  line-height: 1;
  text-shadow: 0 2px 0 rgba(0, 0, 0, 0.75);
}

.poster-reason {
  display: -webkit-box;
  max-width: 930px;
  margin: 16px 0 12px;
  overflow: hidden;
  color: var(--ink);
  font-size: min(1.75vw, 25px);
  line-height: 1.28;
  text-shadow: 0 2px 0 rgba(0, 0, 0, 0.82);
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.poster-reason span {
  color: var(--muted);
}

.poster-sources {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}

.source-caption {
  color: var(--muted);
  font-size: 13px;
  line-height: 1;
}

.source-pill {
  display: inline-flex;
  align-items: center;
  min-height: 28px;
  max-width: 300px;
  padding: 5px 10px;
  overflow: hidden;
  border: 1px solid rgba(0, 240, 79, 0.42);
  border-radius: 6px;
  background: rgba(0, 240, 79, 0.08);
  color: var(--ink);
  cursor: pointer;
  font-size: 13px;
  line-height: 1;
  text-decoration: underline;
  text-underline-offset: 3px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.source-pill:hover,
.source-pill:focus-visible {
  border-color: rgba(0, 240, 79, 0.82);
  background: rgba(0, 240, 79, 0.16);
  color: var(--yes);
  outline: none;
}

.lock-button {
  position: relative;
  display: grid;
  width: min(11.5vw, 134px);
  min-width: 78px;
  aspect-ratio: 1 / 0.76;
  place-items: center;
  border: 1px solid rgba(255, 255, 255, 0.23);
  border-radius: 20px;
  background: rgba(255, 255, 255, 0.045);
  box-shadow: inset 0 0 0 1px rgba(0, 0, 0, 0.24), 0 12px 22px rgba(0, 0, 0, 0.26);
}

.lock-icon {
  position: relative;
  display: block;
  width: 38%;
  height: 31%;
  border: 3px solid rgba(255, 255, 255, 0.74);
  border-radius: 3px;
}

.lock-icon::before {
  content: "";
  position: absolute;
  left: 50%;
  bottom: 78%;
  width: 54%;
  height: 78%;
  border: 3px solid rgba(255, 255, 255, 0.74);
  border-bottom: 0;
  border-radius: 18px 18px 0 0;
  transform: translateX(-50%);
}

@media (max-width: 900px) {
  .forecast-poster {
    min-height: 430px;
  }

  .poster-header h1 {
    font-size: 30px;
  }

  .poster-meta {
    gap: 18px;
    font-size: 22px;
  }

  .poster-odds {
    top: 38%;
    left: 15%;
    right: 15%;
  }

  .odds-model {
    font-size: 16px;
  }

  .poster-closed {
    top: 39%;
  }

  .outcome-label {
    font-size: 34px;
  }

  .odds-side strong {
    font-size: 70px;
  }

  .poster-date {
    font-size: 28px;
  }

  .poster-reason {
    font-size: 16px;
  }
}

@media (max-width: 560px) {
  .site-shell {
    width: min(100vw - 20px, 1040px);
    padding: 24px 0;
  }

  .site-header h1 {
    font-size: 30px;
  }

  .market-row {
    grid-template-columns: 1fr;
    gap: 8px;
  }

  .row-probs {
    justify-content: space-between;
  }

  .forecast-poster {
    min-height: 390px;
  }

  .poster-header {
    width: 91%;
  }

  .poster-header h1 {
    font-size: 20px;
    -webkit-line-clamp: 3;
  }

  .poster-meta {
    margin-top: 12px;
    font-size: 15px;
  }

  .poster-odds {
    left: 8%;
    right: 8%;
    top: 42%;
    column-gap: 4%;
    row-gap: 10px;
    grid-template-columns: minmax(106px, 1fr) 1px minmax(106px, 1fr);
  }

  .odds-model {
    font-size: 12px;
  }

  .poster-closed {
    left: 8%;
    right: 8%;
    top: 40%;
    gap: 12px;
  }

  .poster-closed span {
    font-size: 34px;
  }

  .poster-closed strong {
    font-size: 18px;
  }

  .split-line {
    height: 104px;
  }

  .odds-side {
    gap: 18px;
  }

  .outcome-label {
    font-size: 23px;
  }

  .odds-side strong {
    font-size: 44px;
  }

  .poster-footer {
    grid-template-columns: 1fr auto;
    gap: 14px;
  }

  .poster-date {
    font-size: 20px;
  }

  .poster-reason {
    margin: 9px 0;
    font-size: 12px;
  }

  .source-pill {
    max-width: 130px;
    min-height: 24px;
    font-size: 11px;
  }

  .lock-button {
    min-width: 58px;
    border-radius: 14px;
  }
}

.preview-shell .forecast-poster {
  aspect-ratio: 2 / 1;
  min-height: 600px;
}

.preview-shell .poster-watermark {
  top: 48%;
  font-size: 330px;
}

.preview-shell .poster-header {
  left: 4%;
  top: 4%;
  width: 82%;
}

.preview-shell .poster-header h1 {
  font-size: 35px;
  line-height: 1.16;
  -webkit-line-clamp: 2;
}

.preview-shell .poster-meta {
  gap: 28px;
  margin-top: 16px;
  font-size: 26px;
}

.preview-shell .poster-odds {
  left: 18%;
  right: 18%;
  top: 37%;
  column-gap: 5%;
  row-gap: 18px;
}

.preview-shell .odds-model {
  font-size: 22px;
}

.preview-shell .poster-closed {
  left: 14%;
  right: 14%;
  top: 38%;
}

.preview-shell .poster-closed span {
  font-size: 70px;
}

.preview-shell .poster-closed strong {
  font-size: 34px;
}

.preview-shell .split-line {
  height: 145px;
}

.preview-shell .odds-side {
  gap: 22px;
}

.preview-shell .outcome-label {
  font-size: 40px;
}

.preview-shell .odds-side strong {
  font-size: 86px;
}

.preview-shell .poster-footer {
  left: 4%;
  right: 4%;
  bottom: 4.2%;
}

.preview-shell .poster-date {
  font-size: 30px;
}

.preview-shell .poster-reason {
  max-width: 890px;
  margin: 12px 0 10px;
  font-size: 20px;
  line-height: 1.22;
}

.preview-shell .source-pill {
  max-width: 250px;
  min-height: 24px;
  font-size: 12px;
}

.preview-shell .lock-button {
  width: 112px;
  min-width: 112px;
}
"""
    write_text(os.path.join(output_dir, "assets", "styles.css"), css)


def build_site(input_csv: str, output_dir: str, clean: bool) -> dict[str, Any]:
    rows = load_rows(input_csv)
    if clean and os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    build_css(output_dir)
    page_urls = {}
    for row in rows:
        page_urls[row["id"]] = build_market_page(row, output_dir)
        build_link_preview_page(row, output_dir)
    build_index(rows, page_urls, output_dir)
    build_json(rows, page_urls, output_dir)
    write_text(os.path.join(output_dir, ".nojekyll"), "")

    return {
        "inputCsv": input_csv,
        "outputDir": output_dir,
        "marketPages": len(rows),
        "index": os.path.join(output_dir, "index.html"),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build GitHub Pages forecast cards from MarketNewsPredictions CSV."
    )
    parser.add_argument(
        "--input",
        default=None,
        help="Prediction CSV. Defaults to MarketNewsPredictions.csv, then samples.",
    )
    parser.add_argument(
        "--output-dir",
        default=DOCS_DIR,
        help=f"Static site output directory. Default: {DOCS_DIR}",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Do not delete the output directory before building.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_csv = resolve_input(args.input)
    summary = build_site(input_csv, args.output_dir, clean=not args.no_clean)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
