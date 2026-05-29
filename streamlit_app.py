from __future__ import annotations

import csv
import html
import json
import os
import re
from datetime import datetime
from urllib.parse import quote
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import streamlit as st


PREDICTION_INPUT_CSV = os.environ.get(
    "PREDICTION_CSV_PATH",
    os.path.join("Markets", "MarketNewsPredictions.csv"),
)
DISPLAY_TIMEZONE = ZoneInfo("America/New_York")
STREAMLIT_DIRECT_PATH = os.environ.get(
    "STREAMLIT_DIRECT_PATH",
    "/~/+/" if os.name != "nt" else "/",
)
BLOCKED_SOURCE_DOMAINS = {
    "manifold.markets",
    "polymarket.com",
    "kalshi.com",
    "predictit.org",
    "metaculus.com",
    "merriam-webster.com",
    "dictionary.com",
    "collinsdictionary.com",
    "urbandictionary.com",
}


def h(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def parse_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_optional_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pct(value: object) -> str:
    return f"{round(parse_float(value) * 100)}%"


def first_present(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = row.get(key, "")
        if str(value).strip():
            return str(value)
    return ""


def predicted_yes_value(row: dict[str, str]) -> str:
    return first_present(
        row,
        "newsPredictedYesProbability",
    )


def predicted_no_value(row: dict[str, str]) -> str:
    return first_present(
        row,
        "newsPredictedNoProbability",
    )


def prediction_reason_value(row: dict[str, str]) -> str:
    return first_present(row, "newsShortReason")


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


def direct_reason(value: str, max_chars: int = 190) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    if not text:
        return "No prediction reason available."
    first_sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0].strip()
    text = first_sentence or text
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def source_label(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host or url


def source_url_allowed(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return bool(host) and not any(
        host == domain or host.endswith(f".{domain}")
        for domain in BLOCKED_SOURCE_DOMAINS
    )


def prediction_source_urls(row: dict[str, str], limit: int = 2) -> list[str]:
    urls: list[str] = []

    def add_url(value: object) -> None:
        text = str(value or "").strip()
        if not text:
            return
        if text.startswith("[") and "](" in text and text.endswith(")"):
            text = text.rsplit("](", maxsplit=1)[-1][:-1]
        parsed = urlsplit(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return
        if not source_url_allowed(text):
            return
        if text not in urls:
            urls.append(text)

    for url in split_pipe(row.get("newsSourceUrls", "")):
        add_url(url)

    if len(urls) < limit:
        try:
            raw = json.loads(row.get("newsRawJson", "") or "{}")
        except json.JSONDecodeError:
            raw = {}
        source_url_groups = []
        if isinstance(raw, dict):
            source_url_groups.extend(
                [
                    raw.get("news", {}).get("source_urls", []),
                    raw.get("raw_model_json", {}).get("source_urls", []),
                    raw.get("signals", {}).get("search", {}).get("source_urls", []),
                ]
            )
        for source_urls in source_url_groups:
            if not isinstance(source_urls, list):
                continue
            for url in source_urls:
                add_url(url)
                if len(urls) >= limit:
                    break
            if len(urls) >= limit:
                break

    return urls[:limit]


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
        bool(predicted_yes_value(row)) or is_market_closed(row)
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


def probability_band(yes_probability: float) -> str:
    if yes_probability >= 0.65:
        return "yes-lean"
    if yes_probability <= 0.35:
        return "no-lean"
    return "uncertain"


@st.cache_data(ttl=300)
def load_rows() -> list[dict[str, str]]:
    if os.path.exists(PREDICTION_INPUT_CSV):
        with open(PREDICTION_INPUT_CSV, newline="", encoding="utf-8") as input_file:
            reader = csv.DictReader(input_file)
            return [row for row in reader if is_displayable_row(row)]
    return []


def compact_source_html(row: dict[str, str]) -> str:
    urls = prediction_source_urls(row, limit=2)
    if not urls:
        market_url = row.get("url", "").strip()
        if market_url:
            return (
                '<span class="source-title">Source</span>'
                f'<a class="source-pill" href="{h(market_url)}" target="_blank" '
                f'rel="noopener noreferrer" title="{h(market_url)}">'
                "Market criteria</a>"
            )
        return (
            '<span class="source-title">Sources</span>'
            '<span class="source-empty">No sources available</span>'
        )

    links = []
    for index, url in enumerate(urls[:2], start=1):
        links.append(
            f'<a class="source-pill" href="{h(url)}" target="_blank" '
            f'rel="noopener noreferrer" title="{h(url)}">'
            f"Source {index}: {h(source_label(url))}</a>"
        )
    return '<span class="source-title">Sources</span>\n' + "\n".join(links)


def card_html(row: dict[str, str], embed: bool = False) -> str:
    is_closed = is_market_closed(row)
    yes = parse_float(predicted_yes_value(row))
    no = parse_float(predicted_no_value(row), 1 - yes)
    yes_score_class = "higher-score" if yes >= no else "lower-score"
    no_score_class = "higher-score" if no > yes else "lower-score"
    band = "closed" if is_closed else probability_band(yes)
    question = row.get("question", "").strip()
    model = row.get("forecastModel", "").strip()
    if is_closed:
        forecast_date = f"Closed: {market_close_label(row)}"
        time_label = forecast_date
        reason = "Market closed. No prediction generated."
    else:
        forecast_date = format_edt_timestamp(
            row.get("forecastTimestamp", ""),
            row.get("forecastCurrentDate", "").strip(),
        )
        time_label = f"updated: {forecast_date}"
        reason = direct_reason(prediction_reason_value(row).strip())
    market_url = row.get("url", "").strip()
    mode_class = "embed-view" if embed else "full-view"
    odds_html = (
        """
  <section class="market-status closed-status" aria-label="Market status">
    <span>Market closed</span>
    <strong>No prediction</strong>
  </section>"""
        if is_closed
        else f"""
  <section class="probability-panel" aria-label="Forecast probabilities">
    <div class="probability-cell yes-cell">
      <span class="outcome-label {yes_score_class}">Yes</span>
      <strong class="{yes_score_class}">{h(pct(yes))}</strong>
    </div>
    <div class="probability-divider" aria-hidden="true">|</div>
    <div class="probability-cell no-cell">
      <span class="outcome-label {no_score_class}">No</span>
      <strong class="{no_score_class}">{h(pct(no))}</strong>
    </div>
  </section>"""
    )
    sources_html = (
        ""
        if is_closed
        else f"""
  <section class="source-row" aria-label="Sources">
    {compact_source_html(row)}
  </section>"""
    )

    market_link = (
        f'<a class="market-link" href="{h(market_url)}" target="_blank" '
        f'rel="noopener noreferrer">Market</a>'
        if market_url
        else ""
    )

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {{
  color-scheme: light;
  --bg: #f3f1eb;
  --card: #fffefa;
  --ink: #16181d;
  --muted: #656b75;
  --soft: #eef0f2;
  --line: #d9dde3;
  --yes: #0aa34f;
  --no: #4f5865;
  --danger: #b42318;
  --accent: #24324a;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; background: var(--bg); color: var(--ink); }}
.forecast-card {{
  width: 100%;
  max-width: 100vw;
  min-height: 290px;
  padding: 16px;
  display: grid;
  grid-template-rows: auto auto auto 1fr;
  gap: 10px;
  overflow: hidden;
  background: linear-gradient(180deg, #fffefa 0%, #f9faf8 100%);
  border: 1px solid var(--line);
  border-top: 4px solid var(--accent);
  border-radius: 8px;
  box-shadow: 0 12px 28px rgba(18, 24, 34, 0.10);
  color: var(--ink);
}}
.forecast-card.closed {{
  border-top-color: var(--danger);
}}
.card-header {{
  display: grid;
  gap: 7px;
  min-width: 0;
}}
.card-header h1 {{
  display: -webkit-box;
  min-width: 0;
  margin: 0;
  overflow: hidden;
  color: var(--ink);
  font-size: 18px;
  font-weight: 720;
  line-height: 1.18;
  letter-spacing: 0;
  overflow-wrap: anywhere;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}}
.meta-row {{
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.1;
}}
.meta-row span, .market-link {{
  min-width: 0;
  max-width: 100%;
  padding: 4px 7px;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: #ffffff;
  text-overflow: ellipsis;
  white-space: nowrap;
}}
.market-link {{
  color: var(--accent);
  text-decoration: none;
}}
.probability-panel {{
  display: grid;
  min-width: 0;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #f8faf9;
}}
.probability-cell {{
  display: grid;
  gap: 4px;
  min-width: 0;
}}
.no-cell {{ text-align: right; }}
.outcome-label {{
  font-size: 22px;
  font-weight: 780;
  line-height: 1;
  text-transform: uppercase;
}}
.probability-cell strong {{
  font-size: 36px;
  font-weight: 820;
  line-height: 1;
}}
.probability-divider {{
  color: #a0a7b2;
  font-size: 30px;
  font-weight: 500;
  line-height: 1;
}}
.probability-cell strong.higher-score {{ color: var(--yes); }}
.probability-cell strong.lower-score {{ color: var(--no); }}
.outcome-label.higher-score {{ color: var(--yes); }}
.outcome-label.lower-score {{ color: var(--no); }}
.market-status {{
  display: grid;
  gap: 5px;
  padding: 18px 14px;
  border: 1px solid #f1b4ae;
  border-radius: 8px;
  background: #fff7f5;
  text-align: center;
}}
.market-status span {{
  color: var(--danger);
  font-size: 22px;
  font-weight: 800;
  text-transform: uppercase;
}}
.market-status strong {{
  color: var(--muted);
  font-size: 14px;
  font-weight: 650;
}}
.reason-block {{
  min-width: 0;
  padding: 10px 12px;
  border-left: 3px solid var(--accent);
  background: #f6f7f8;
}}
.reason-label {{
  display: block;
  margin-bottom: 4px;
  color: var(--muted);
  font-size: 10px;
  font-weight: 760;
  line-height: 1;
  text-transform: uppercase;
}}
.reason-block p {{
  display: -webkit-box;
  min-width: 0;
  margin: 0;
  overflow: hidden;
  color: var(--ink);
  font-size: 13px;
  font-weight: 560;
  line-height: 1.28;
  overflow-wrap: anywhere;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
}}
.card-footer {{
  display: grid;
  gap: 7px;
  min-width: 0;
}}
.updated-line {{
  color: var(--muted);
  font-size: 10px;
  line-height: 1;
}}
.source-row {{
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) minmax(0, 1fr);
  gap: 6px;
  align-items: center;
  min-width: 0;
  overflow: hidden;
}}
.source-title {{
  color: var(--muted);
  font-size: 10px;
  font-weight: 760;
  line-height: 1;
  text-transform: uppercase;
  white-space: nowrap;
}}
.source-pill {{
  display: inline-flex;
  align-items: center;
  min-width: 0;
  max-width: 100%;
  min-height: 23px;
  padding: 4px 8px;
  overflow: hidden;
  border: 1px solid #c8d1db;
  border-radius: 6px;
  background: #ffffff;
  color: var(--accent);
  cursor: pointer;
  font-size: 10px;
  font-weight: 650;
  line-height: 1;
  text-decoration: none;
  text-overflow: ellipsis;
  white-space: nowrap;
}}
.source-pill:hover, .source-pill:focus-visible {{
  border-color: var(--accent);
  background: #f2f5f8;
  outline: none;
}}
.source-empty {{
  grid-column: span 2;
  min-width: 0;
  overflow: hidden;
  color: var(--muted);
  font-size: 10px;
  line-height: 1;
  text-overflow: ellipsis;
  white-space: nowrap;
}}
body.full-view .forecast-card {{
  min-height: 500px;
  padding: 34px;
  gap: 24px;
}}
body.full-view .card-header h1 {{ font-size: min(3vw, 38px); }}
body.full-view .meta-row {{ font-size: 14px; gap: 8px; }}
body.full-view .probability-panel {{ padding: 28px 34px; gap: 26px; }}
body.full-view .outcome-label {{ font-size: min(2.4vw, 38px); }}
body.full-view .probability-cell strong {{ font-size: min(6.6vw, 112px); }}
body.full-view .probability-divider {{ font-size: min(4vw, 58px); }}
body.full-view .reason-block {{ padding: 18px 20px; }}
body.full-view .reason-label {{ font-size: 13px; }}
body.full-view .reason-block p {{ font-size: min(1.7vw, 24px); line-height: 1.32; }}
body.full-view .updated-line {{ font-size: 14px; }}
body.full-view .source-pill {{ min-height: 30px; max-width: 280px; font-size: 13px; }}
body.embed-view .forecast-card {{
  width: min(100%, 390px);
  max-width: 100%;
  min-height: 280px;
  padding: 11px;
  gap: 8px;
  border-radius: 0;
  box-shadow: none;
}}
@media (max-width: 520px) {{
  body.embed-view .forecast-card {{ min-height: 278px; padding: 9px 10px; gap: 7px; }}
  body.embed-view .card-header h1 {{
    font-size: 13px;
    line-height: 1.18;
    -webkit-line-clamp: 2;
  }}
  body.embed-view .meta-row {{ font-size: 9px; }}
  body.embed-view .meta-row span, body.embed-view .market-link {{ padding: 3px 6px; }}
  body.embed-view .probability-panel {{ padding: 9px 10px; gap: 8px; }}
  body.embed-view .outcome-label {{ font-size: 16px; }}
  body.embed-view .probability-cell strong {{ font-size: 30px; }}
  body.embed-view .probability-divider {{ font-size: 24px; }}
  body.embed-view .reason-block {{ padding: 8px 10px; }}
  body.embed-view .reason-label {{ font-size: 9px; }}
  body.embed-view .reason-block p {{ font-size: 10px; line-height: 1.2; -webkit-line-clamp: 2; }}
  body.embed-view .updated-line {{ font-size: 9px; }}
  body.embed-view .source-title, body.embed-view .source-empty {{ font-size: 9px; }}
  body.embed-view .source-pill {{ min-height: 20px; padding: 3px 6px; font-size: 9px; }}
}}
@media (max-width: 900px) {{
  body.full-view .forecast-card {{ min-height: 430px; padding: 24px; }}
  body.full-view .card-header h1 {{ font-size: 28px; }}
  body.full-view .probability-cell strong {{ font-size: 66px; }}
  body.full-view .reason-block p {{ font-size: 16px; }}
}}
</style>
</head>
<body class="{mode_class}">
<article class="forecast-card {band}">
  <header class="card-header">
    <h1>{h(question)}</h1>
    <div class="meta-row">
      <span>model: {h(model or "unknown")}</span>
      <span class="updated-line">{h(time_label)}</span>
      {market_link}
    </div>
  </header>
{odds_html}
{sources_html}
  <section class="reason-block" aria-label="Forecast reason">
    <span class="reason-label">Reason</span>
    <p>{h(reason)}</p>
  </section>
</article>
</body>
</html>"""


def card_fragment(row: dict[str, str], embed: bool = False) -> str:
    document = card_html(row, embed=embed)
    style_match = re.search(r"<style>(.*?)</style>", document, flags=re.DOTALL)
    body_match = re.search(r"<body class=\"([^\"]+)\">(.*?)</body>", document, flags=re.DOTALL)
    if not style_match or not body_match:
        return document

    css = style_match.group(1)
    mode_class = body_match.group(1)
    body = body_match.group(2)
    css = css.replace(
        "html, body { margin: 0; background: var(--bg); color: var(--ink); }",
        ".forecast-card-root { margin: 0; background: var(--bg); color: var(--ink); }",
    )
    css = css.replace("body.full-view", ".forecast-card-root.full-view")
    css = css.replace("body.embed-view", ".forecast-card-root.embed-view")
    return f"""
<style>
{css}
.forecast-card-root {{
  width: 100%;
  max-width: 100%;
  overflow: hidden;
}}
</style>
<div class="forecast-card-root {h(mode_class)}">
{body}
</div>
"""


def selected_market_id() -> str:
    return st.query_params.get("market") or st.query_params.get("id") or ""


def selected_market(rows: list[dict[str, str]], market_id: str) -> dict[str, str] | None:
    for row in rows:
        if row.get("id") == market_id:
            return row
    return None


def is_embed_request() -> bool:
    return (
        st.query_params.get("mode") == "embed"
        or st.query_params.get("compact") == "1"
        or st.query_params.get("embed") == "true"
        or st.query_params.get("embedded") == "true"
    )


def render_directory(rows: list[dict[str, str]]) -> None:
    total = len(rows)
    closed_count = sum(1 for row in rows if is_market_closed(row))
    active_count = total - closed_count
    latest_timestamp = max(
        (row.get("forecastTimestamp", "") for row in rows if row.get("forecastTimestamp")),
        default="",
    )
    latest_update = format_edt_timestamp(latest_timestamp, "No predictions loaded")
    st.markdown(
        """
        <style>
        html, body, .stApp {
          background: #f3f1eb !important;
        }
        .directory-dashboard {
          min-height: 100vh;
          padding: 26px;
          background: #f3f1eb;
          color: #16181d;
          font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        }
        .dashboard-shell {
          width: min(1160px, 100%);
          margin: 0 auto;
        }
        .dashboard-header {
          display: grid;
          grid-template-columns: minmax(0, 1fr) auto;
          gap: 20px;
          align-items: end;
          margin-bottom: 18px;
        }
        .dashboard-kicker {
          margin: 0 0 6px;
          color: #656b75;
          font-size: 12px;
          font-weight: 760;
          letter-spacing: 0.08em;
          text-transform: uppercase;
        }
        .dashboard-header h1 {
          margin: 0;
          color: #16181d;
          font-size: clamp(28px, 4vw, 44px);
          line-height: 1.05;
          letter-spacing: 0;
        }
        .dashboard-subtitle {
          margin: 10px 0 0;
          max-width: 720px;
          color: #656b75;
          font-size: 15px;
          line-height: 1.45;
        }
        .summary-grid {
          display: grid;
          grid-template-columns: repeat(3, minmax(88px, 1fr));
          gap: 8px;
          min-width: 320px;
        }
        .summary-card {
          padding: 10px 12px;
          border: 1px solid #d9dde3;
          border-radius: 8px;
          background: #fffefa;
          box-shadow: 0 8px 20px rgba(18, 24, 34, 0.06);
        }
        .summary-card span {
          display: block;
          margin-bottom: 4px;
          color: #656b75;
          font-size: 10px;
          font-weight: 760;
          text-transform: uppercase;
        }
        .summary-card strong {
          display: block;
          color: #16181d;
          font-size: 20px;
          line-height: 1;
        }
        .run-meta {
          margin: 0 0 16px;
          color: #656b75;
          font-size: 12px;
        }
        .dashboard-list {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
          gap: 12px;
        }
        .dashboard-card {
          display: grid;
          gap: 12px;
          min-height: 238px;
          padding: 14px;
          border: 1px solid #d9dde3;
          border-top: 4px solid #24324a;
          border-radius: 8px;
          background: #fffefa;
          box-shadow: 0 10px 24px rgba(18, 24, 34, 0.07);
        }
        .dashboard-card.closed {
          border-top-color: #b42318;
        }
        .dashboard-card .card-title {
          display: block;
          margin: 0;
          overflow: visible;
          color: #16181d;
          font-size: 12.5px !important;
          font-weight: 680;
          line-height: 1.34 !important;
          overflow-wrap: anywhere;
        }
        .card-meta {
          display: flex;
          flex-wrap: wrap;
          gap: 6px;
          color: #656b75;
          font-size: 10px;
        }
        .card-meta span {
          padding: 3px 7px;
          border: 1px solid #d9dde3;
          border-radius: 999px;
          background: #ffffff;
        }
        .mini-probs {
          display: grid;
          grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
          gap: 10px;
          align-items: center;
          padding: 10px 12px;
          border: 1px solid #d9dde3;
          border-radius: 8px;
          background: #f8faf9;
        }
        .mini-prob {
          display: grid;
          gap: 3px;
        }
        .mini-prob.no {
          text-align: right;
        }
        .mini-prob span {
          color: #4f5865;
          font-size: 11px;
          font-weight: 780;
          text-transform: uppercase;
        }
        .mini-prob strong {
          color: #4f5865;
          font-size: 28px;
          line-height: 1;
        }
        .mini-prob.high span,
        .mini-prob.high strong {
          color: #0aa34f;
        }
        .mini-divider {
          color: #a0a7b2;
          font-size: 22px;
        }
        .closed-note {
          padding: 14px 12px;
          border: 1px solid #f1b4ae;
          border-radius: 8px;
          background: #fff7f5;
          color: #b42318;
          font-size: 14px;
          font-weight: 780;
          text-transform: uppercase;
          text-align: center;
        }
        .card-reason {
          display: -webkit-box;
          min-height: 44px;
          margin: 0;
          overflow: hidden;
          color: #16181d;
          font-size: 12px;
          line-height: 1.3;
          overflow-wrap: anywhere;
          -webkit-box-orient: vertical;
          -webkit-line-clamp: 3;
        }
        .card-actions {
          display: flex;
          gap: 8px;
          align-items: center;
          margin-top: auto;
        }
        .card-actions a {
          display: inline-flex;
          align-items: center;
          min-height: 28px;
          padding: 5px 10px;
          border: 1px solid #c8d1db;
          border-radius: 6px;
          background: #ffffff;
          color: #24324a;
          font-size: 12px;
          font-weight: 680;
          text-decoration: none;
        }
        .card-actions a:hover, .card-actions a:focus-visible {
          border-color: #24324a;
          background: #f2f5f8;
          outline: none;
        }
        .empty-state {
          padding: 18px;
          border: 1px solid #d9dde3;
          border-radius: 8px;
          background: #fffefa;
          color: #656b75;
        }
        @media (max-width: 720px) {
          .directory-dashboard {
            padding: 18px 12px;
          }
          .dashboard-header {
            grid-template-columns: 1fr;
          }
          .summary-grid {
            min-width: 0;
          }
          .dashboard-list {
            grid-template-columns: 1fr;
          }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    cards = []
    for row in rows:
        market_id = row.get("id", "")
        normal_href = f"{STREAMLIT_DIRECT_PATH}?market={quote(market_id)}"
        embed_href = (
            f"{STREAMLIT_DIRECT_PATH}?market={quote(market_id)}"
            "&mode=embed&embedded=true"
        )
        model = row.get("forecastModel", "").strip() or "unknown"
        updated = format_edt_timestamp(
            row.get("forecastTimestamp", ""),
            row.get("forecastCurrentDate", ""),
        )
        reason = direct_reason(prediction_reason_value(row))
        if is_market_closed(row):
            status_class = "closed"
            prob_html = '<div class="closed-note">Market closed</div>'
            reason = "Market closed. No prediction generated."
        else:
            status_class = ""
            yes = parse_float(predicted_yes_value(row))
            no = parse_float(predicted_no_value(row), 1 - yes)
            yes_high = " high" if yes >= no else ""
            no_high = " high" if no > yes else ""
            prob_html = (
                '<div class="mini-probs">'
                f'<div class="mini-prob yes{yes_high}"><span>Yes</span><strong>{h(pct(yes))}</strong></div>'
                '<div class="mini-divider">|</div>'
                f'<div class="mini-prob no{no_high}"><span>No</span><strong>{h(pct(no))}</strong></div>'
                "</div>"
            )
        cards.append(
            f"""
          <article class="dashboard-card {status_class}">
            <div class="card-title">{h(row.get("question", ""))}</div>
            <div class="card-meta">
              <span>model: {h(model)}</span>
              <span>{h(updated)}</span>
            </div>
            {prob_html}
            <p class="card-reason">{h(reason)}</p>
            <div class="card-actions">
              <a href="{h(normal_href)}">Open card</a>
              <a href="{h(embed_href)}">Embed view</a>
            </div>
          </article>"""
        )

    cards_html = (
        "".join(cards)
        if cards
        else '<div class="empty-state">No predictions are currently available.</div>'
    )

    st.markdown(
        f"""
        <main class="directory-dashboard">
          <div class="dashboard-shell">
            <header class="dashboard-header">
              <div>
                <p class="dashboard-kicker">News-search forecasts</p>
                <h1>Manifold Prediction Dashboard</h1>
                <p class="dashboard-subtitle">Current generated forecasts with compact embed pages for Manifold comments.</p>
              </div>
              <section class="summary-grid" aria-label="Prediction summary">
                <div class="summary-card"><span>Total</span><strong>{total}</strong></div>
                <div class="summary-card"><span>Active</span><strong>{active_count}</strong></div>
                <div class="summary-card"><span>Closed</span><strong>{closed_count}</strong></div>
              </section>
            </header>
            <p class="run-meta">Latest update: {h(latest_update)}</p>
            <section class="dashboard-list" aria-label="Markets">
              {cards_html}
            </section>
          </div>
        </main>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(
        page_title="Manifold Prediction Card",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.markdown(
        """
        <style>
        html, body, .stApp {
          margin: 0 !important;
          background: #050406 !important;
        }
        #MainMenu,
        footer,
        [data-testid="stHeader"],
        [data-testid="stToolbar"],
        [data-testid="stDecoration"],
        [data-testid="stStatusWidget"],
        [data-testid="stMainMenu"],
        [data-testid="stDeployButton"],
        [data-testid="stAppDeployButton"],
        .stDeployButton,
        .viewerBadge_container__1QSob,
        .viewerBadge_link__1S137,
        a[href*="streamlit.io/cloud"],
        a[href*="streamlit.io"] {
          display: none !important;
          visibility: hidden !important;
          pointer-events: none !important;
        }
        div[data-testid="stAppViewBlockContainer"] + div {
          display: none !important;
          visibility: hidden !important;
          pointer-events: none !important;
        }
        .block-container {
          padding: 0 !important;
          max-width: none !important;
        }
        [data-testid="stVerticalBlock"],
        [data-testid="stElementContainer"],
        [data-testid="stMarkdownContainer"] {
          gap: 0 !important;
          margin: 0 !important;
          padding: 0 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    rows = load_rows()
    market_id = selected_market_id()
    if not market_id:
        render_directory(rows)
        return

    row = selected_market(rows, market_id)
    if row is None:
        st.error("No matching market prediction found.")
        return

    is_embed = is_embed_request()
    fragment = card_fragment(row, embed=is_embed)
    if hasattr(st, "html"):
        st.html(fragment)
    else:
        st.markdown(fragment, unsafe_allow_html=True)

    if st.query_params.get("list") == "1":
        st.write("Available market ids:")
        for item in rows:
            st.write(f"- `{item.get('id')}`: {item.get('question')}")


if __name__ == "__main__":
    main()
