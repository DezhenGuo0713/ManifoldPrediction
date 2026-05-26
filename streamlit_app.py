from __future__ import annotations

import csv
import html
import os
import re
from datetime import datetime
from urllib.parse import quote
from urllib.parse import urlsplit
from urllib.request import urlopen
from zoneinfo import ZoneInfo

import streamlit as st


REMOTE_INPUTS = [
    os.environ.get("PREDICTION_CSV_URL", ""),
    (
        "https://raw.githubusercontent.com/"
        "DezhenGuo0713/ManifoldPrediction/main/Markets/MarketNewsPredictions.csv"
    ),
]
PREFERRED_INPUTS = [
    os.path.join("Markets", "MarketNewsPredictions.csv"),
    os.path.join("Markets", "MarketNewsPredictions.10_sample.csv"),
    os.path.join("Markets", "MarketNewsPredictions.sample.csv"),
]
DISPLAY_TIMEZONE = ZoneInfo("America/New_York")


def h(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def parse_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def pct(value: object) -> str:
    return f"{round(parse_float(value) * 100)}%"


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


def source_label(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host or url


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
    for url in REMOTE_INPUTS:
        if not url:
            continue
        try:
            with urlopen(url, timeout=15) as response:
                text = response.read().decode("utf-8")
            reader = csv.DictReader(text.splitlines())
            rows = [
                row
                for row in reader
                if row.get("id") and row.get("newsPredictedYesProbability")
            ]
            if rows:
                return rows
        except Exception:
            pass

    for path in PREFERRED_INPUTS:
        if os.path.exists(path):
            with open(path, newline="", encoding="utf-8") as input_file:
                reader = csv.DictReader(input_file)
                return [
                    row
                    for row in reader
                    if row.get("id") and row.get("newsPredictedYesProbability")
                ]
    return []


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


def card_html(row: dict[str, str], embed: bool = False) -> str:
    yes = parse_float(row.get("newsPredictedYesProbability"))
    no = parse_float(row.get("newsPredictedNoProbability"), 1 - yes)
    yes_score_class = "higher-score" if yes >= no else "lower-score"
    no_score_class = "higher-score" if no > yes else "lower-score"
    band = probability_band(yes)
    question = row.get("question", "").strip()
    confidence = (row.get("newsConfidence", "").strip() or "unknown").upper()
    source_count = min(2, len(split_pipe(row.get("newsSourceUrls", ""))))
    model = row.get("forecastModel", "").strip()
    forecast_date = format_edt_timestamp(
        row.get("forecastTimestamp", ""),
        row.get("forecastCurrentDate", "").strip(),
    )
    reason = row.get("newsShortReason", "").strip()
    market_url = row.get("url", "").strip()
    symbol = market_symbol(question)
    mode_class = "embed-view" if embed else "full-view"

    market_button = (
        f'<a class="lock-button" href="{h(market_url)}" target="_blank" '
        f'rel="noopener noreferrer" aria-label="Open Manifold market">'
        f'<span class="lock-icon" aria-hidden="true"></span></a>'
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
  color-scheme: dark;
  --bg: #050406;
  --ink: #f7f7f2;
  --muted: #b8adae;
  --line: rgba(255, 255, 255, 0.16);
  --yes: #00f04f;
  --no: #f4f0e8;
  font-family: "Courier New", Courier, ui-monospace, monospace;
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; background: var(--bg); color: var(--ink); }}
.forecast-poster {{
  position: relative;
  width: 100%;
  height: 320px;
  min-height: 0;
  overflow: hidden;
  background: #090407;
  border: 1px solid rgba(255, 255, 255, 0.08);
  color: var(--ink);
  isolation: isolate;
}}
.forecast-poster::before {{
  content: "";
  position: absolute;
  inset: 0;
  background: #090407;
  box-shadow: inset 0 0 180px rgba(111, 45, 8, 0.22), inset 0 0 0 9999px rgba(0, 0, 0, 0.18);
  z-index: -3;
}}
.poster-watermark {{
  position: absolute;
  left: 50%;
  top: 48%;
  transform: translate(-50%, -50%) rotate(10deg);
  color: rgba(255, 255, 255, 0.13);
  font-size: clamp(150px, 28vw, 250px);
  font-weight: 900;
  line-height: 1;
  z-index: -1;
  user-select: none;
}}
.poster-header {{
  position: absolute;
  left: 4%;
  top: 12px;
  width: min(82%, 760px);
}}
.poster-header h1 {{
  display: -webkit-box;
  margin: 0;
  overflow: hidden;
  color: var(--ink);
  font-size: clamp(17px, 2.8vw, 25px);
  font-weight: 800;
  line-height: 1.18;
  text-shadow: 0 3px 0 rgba(0, 0, 0, 0.65);
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}}
.poster-meta {{
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  margin-top: 10px;
  color: var(--muted);
  font-size: clamp(13px, 2vw, 20px);
  line-height: 1.15;
  text-shadow: 0 2px 0 rgba(0, 0, 0, 0.8);
}}
.poster-odds {{
  position: absolute;
  left: 14%;
  right: 14%;
  top: 34%;
  display: grid;
  grid-template-columns: minmax(96px, 1fr) 1px minmax(96px, 1fr);
  align-items: center;
  gap: 5%;
}}
.split-line {{
  width: 1px;
  height: 88px;
  background: rgba(255, 255, 255, 0.28);
}}
.odds-side {{ display: grid; gap: 8px; }}
.no-side {{ text-align: right; }}
.outcome-label {{
  color: var(--ink);
  font-size: clamp(19px, 3vw, 34px);
  font-weight: 700;
  line-height: 1;
  text-shadow: 0 3px 0 rgba(0, 0, 0, 0.8);
}}
.odds-side strong {{
  font-size: clamp(40px, 6.5vw, 70px);
  font-weight: 900;
  line-height: 0.95;
  text-shadow: 0 6px 0 rgba(0, 0, 0, 0.78);
}}
.odds-side strong.higher-score {{ color: var(--yes); }}
.odds-side strong.lower-score {{ color: var(--no); }}
.outcome-label.higher-score {{ color: var(--yes); }}
.outcome-label.lower-score {{ color: var(--no); }}
.poster-footer {{
  position: absolute;
  left: 4%;
  right: 4%;
  bottom: 14px;
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 18px;
  align-items: end;
}}
.footer-copy {{
  min-width: 0;
  overflow: hidden;
}}
.poster-date {{
  display: block;
  color: var(--ink);
  font-size: clamp(14px, 2.15vw, 20px);
  line-height: 1;
  text-shadow: 0 2px 0 rgba(0, 0, 0, 0.75);
}}
.poster-reason {{
  display: -webkit-box;
  max-width: 720px;
  margin: 8px 0 8px;
  overflow: hidden;
  color: var(--ink);
  font-size: clamp(11px, 1.45vw, 14px);
  line-height: 1.2;
  text-shadow: 0 2px 0 rgba(0, 0, 0, 0.82);
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}}
.poster-reason span, .source-caption {{ color: var(--muted); }}
.poster-sources {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }}
.source-caption {{ font-size: 11px; line-height: 1; }}
.source-pill {{
  display: inline-flex;
  align-items: center;
  min-height: 22px;
  max-width: 174px;
  padding: 4px 8px;
  overflow: hidden;
  border: 1px solid rgba(0, 240, 79, 0.42);
  border-radius: 6px;
  background: rgba(0, 240, 79, 0.08);
  color: var(--ink);
  cursor: pointer;
  font-size: 11px;
  line-height: 1;
  text-decoration: underline;
  text-underline-offset: 3px;
  text-overflow: ellipsis;
  white-space: nowrap;
}}
.source-pill:hover, .source-pill:focus-visible {{
  border-color: rgba(0, 240, 79, 0.82);
  background: rgba(0, 240, 79, 0.16);
  color: var(--yes);
  outline: none;
}}
.lock-button {{
  position: relative;
  display: grid;
  width: clamp(54px, 9vw, 74px);
  min-width: 54px;
  aspect-ratio: 1 / 0.76;
  place-items: center;
  border: 1px solid rgba(255, 255, 255, 0.23);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.045);
}}
body.full-view .forecast-poster {{
  height: auto;
  min-height: 540px;
  aspect-ratio: 16 / 9;
}}
body.full-view .poster-watermark {{
  top: 49%;
  font-size: min(31vw, 430px);
}}
body.full-view .poster-header {{
  left: 4.5%;
  top: 3.5%;
  width: min(78%, 1080px);
}}
body.full-view .poster-header h1 {{
  font-size: min(3vw, 38px);
  line-height: 1.22;
}}
body.full-view .poster-meta {{
  gap: 34px;
  margin-top: 22px;
  font-size: min(2.2vw, 36px);
}}
body.full-view .poster-odds {{
  left: 21%;
  right: 21%;
  top: 40%;
  grid-template-columns: minmax(180px, 1fr) 1px minmax(180px, 1fr);
  gap: 6%;
}}
body.full-view .split-line {{ height: 190px; }}
body.full-view .odds-side {{ gap: 34px; }}
body.full-view .outcome-label {{ font-size: min(3.1vw, 58px); }}
body.full-view .odds-side strong {{ font-size: min(7.2vw, 130px); }}
body.full-view .poster-footer {{
  left: 4.5%;
  right: 4.5%;
  bottom: 4.4%;
  gap: 30px;
}}
body.full-view .poster-date {{ font-size: min(2.4vw, 44px); }}
body.full-view .poster-reason {{
  max-width: 930px;
  margin: 16px 0 12px;
  font-size: min(1.75vw, 25px);
  line-height: 1.28;
}}
body.full-view .poster-sources {{ gap: 8px; }}
body.full-view .source-caption {{ font-size: 13px; }}
body.full-view .source-pill {{
  min-height: 28px;
  max-width: 300px;
  padding: 5px 10px;
  font-size: 13px;
}}
body.full-view .lock-button {{
  width: min(11.5vw, 134px);
  min-width: 78px;
  border-radius: 20px;
}}
.lock-icon {{
  position: relative;
  display: block;
  width: 38%;
  height: 31%;
  border: 3px solid rgba(255, 255, 255, 0.74);
  border-radius: 3px;
}}
.lock-icon::before {{
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
}}
body.embed-view .forecast-poster {{
  display: grid;
  grid-template-rows: auto 1fr auto;
  gap: 8px;
  width: 100vw;
  max-width: 100vw;
  height: 320px;
  padding: 14px 16px 12px;
}}
body.embed-view .poster-watermark {{
  display: none;
}}
body.embed-view .poster-header {{
  position: relative;
  left: auto;
  top: auto;
  width: min(360px, calc(100vw - 32px));
  max-width: min(360px, calc(100vw - 32px));
}}
body.embed-view .poster-header h1 {{
  font-size: clamp(15px, 2.5vw, 22px);
  line-height: 1.18;
  -webkit-line-clamp: 2;
}}
body.embed-view .poster-meta {{
  display: none;
}}
body.embed-view .poster-odds {{
  position: relative;
  left: auto;
  right: auto;
  top: auto;
  width: min(330px, calc(100vw - 32px));
  margin: 0;
  grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
  gap: 10px;
  align-self: center;
}}
body.embed-view .yes-side {{
  grid-column: 1;
  grid-row: 1;
}}
body.embed-view .split-line {{
  display: grid;
  width: auto;
  height: auto;
  place-items: center;
  background: transparent;
  color: var(--muted);
  font-size: clamp(24px, 5vw, 34px);
  font-weight: 800;
  line-height: 1;
  text-shadow: 0 3px 0 rgba(0, 0, 0, 0.78);
}}
body.embed-view .split-line::before {{
  content: "|";
}}
body.embed-view .no-side {{
  grid-column: 3;
  grid-row: 1;
}}
body.embed-view .odds-side {{
  gap: 6px;
}}
body.embed-view .outcome-label {{
  font-size: clamp(20px, 4.8vw, 30px);
}}
body.embed-view .odds-side strong {{
  font-size: clamp(32px, 7vw, 52px);
}}
body.embed-view .poster-footer {{
  position: relative;
  left: auto;
  right: auto;
  bottom: auto;
  display: block;
  width: min(360px, calc(100vw - 32px));
  max-width: min(360px, calc(100vw - 32px));
}}
body.embed-view .poster-date {{
  margin-bottom: 5px;
  color: var(--muted);
  font-size: 11px;
}}
body.embed-view .poster-reason {{
  max-width: none;
  margin: 0 0 8px;
  font-size: clamp(10px, 1.75vw, 12px);
  line-height: 1.22;
  overflow-wrap: anywhere;
  white-space: normal;
  -webkit-line-clamp: 3;
}}
body.embed-view .poster-sources {{
  flex-wrap: nowrap;
  overflow: hidden;
}}
body.embed-view .source-caption {{
  flex: 0 0 auto;
  font-size: 10px;
}}
body.embed-view .source-pill {{
  min-width: 0;
  max-width: calc((100% - 58px) / 2);
  min-height: 21px;
  padding: 3px 7px;
  font-size: 10px;
}}
body.embed-view .lock-button {{
  display: none;
}}
@media (max-width: 520px) {{
  body.embed-view .forecast-poster {{
    width: 100vw;
    max-width: 100vw;
    padding: 10px 12px;
    gap: 6px;
  }}
  body.embed-view .poster-header,
  body.embed-view .poster-footer {{
    width: min(360px, calc(100vw - 24px));
    max-width: min(360px, calc(100vw - 24px));
  }}
  body.embed-view .poster-header h1 {{
    font-size: 13px;
    line-height: 1.16;
    -webkit-line-clamp: 2;
  }}
  body.embed-view .poster-odds {{
    width: min(330px, calc(100vw - 24px));
    grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
    gap: 9px;
  }}
  body.embed-view .yes-side {{
    grid-column: 1;
    grid-row: 1;
  }}
  body.embed-view .no-side {{
    grid-column: 3;
    grid-row: 1;
  }}
  body.embed-view .odds-side {{ gap: 5px; }}
  body.embed-view .split-line {{ font-size: 24px; }}
  body.embed-view .outcome-label {{ font-size: 18px; }}
  body.embed-view .odds-side strong {{ font-size: 30px; }}
  body.embed-view .poster-date {{ font-size: 10px; }}
  body.embed-view .poster-reason {{
    margin: 0 0 7px;
    font-size: 10px;
    line-height: 1.16;
    -webkit-line-clamp: 3;
  }}
  body.embed-view .poster-sources {{ gap: 5px; }}
  body.embed-view .source-caption {{ display: none; }}
  body.embed-view .source-pill {{
    max-width: calc((100% - 5px) / 2);
    min-height: 20px;
    padding: 3px 6px;
    font-size: 9px;
  }}
}}
@media (max-width: 900px) {{
  body.full-view .forecast-poster {{ min-height: 430px; }}
  body.full-view .poster-header h1 {{ font-size: 30px; }}
  body.full-view .poster-meta {{ gap: 18px; font-size: 22px; }}
  body.full-view .poster-odds {{ top: 38%; left: 15%; right: 15%; }}
  body.full-view .outcome-label {{ font-size: 34px; }}
  body.full-view .odds-side strong {{ font-size: 70px; }}
  body.full-view .poster-date {{ font-size: 28px; }}
  body.full-view .poster-reason {{ font-size: 16px; }}
}}
</style>
</head>
<body class="{mode_class}">
<article class="forecast-poster {band}">
  <div class="poster-watermark" aria-hidden="true">{h(symbol)}</div>
  <header class="poster-header">
    <h1>{h(question)}</h1>
    <div class="poster-meta">
      <span>Conf: {h(confidence)}</span>
      <span>Src: {source_count}</span>
      <span>{h(model)}</span>
    </div>
  </header>
  <section class="poster-odds" aria-label="Forecast probabilities">
    <div class="odds-side yes-side">
      <span class="outcome-label {yes_score_class}">YES</span>
      <strong class="{yes_score_class}">{h(pct(yes))}</strong>
    </div>
    <div class="split-line" aria-hidden="true"></div>
    <div class="odds-side no-side">
      <span class="outcome-label {no_score_class}">NO</span>
      <strong class="{no_score_class}">{h(pct(no))}</strong>
    </div>
  </section>
  <footer class="poster-footer">
    <div class="footer-copy">
      <span class="poster-date">{h(forecast_date)}</span>
      <p class="poster-reason"><span>Reason:</span> {h(reason)}</p>
      <div class="poster-sources" aria-label="Sources">
        <span class="source-caption">Source</span>
        {compact_source_html(row)}
      </div>
    </div>
    {market_button}
  </footer>
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
    st.markdown(
        """
        <style>
        .directory {
          min-height: 100vh;
          padding: 28px;
          background: #050406;
          color: #f7f7f2;
          font-family: "Courier New", Courier, ui-monospace, monospace;
        }
        .directory h1 {
          margin: 0 0 10px;
          font-size: 28px;
          line-height: 1.1;
        }
        .directory p {
          margin: 0 0 22px;
          color: #b8adae;
          font-size: 15px;
        }
        .market-list {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
          gap: 12px;
        }
        .market-link {
          display: block;
          min-height: 136px;
          padding: 14px;
          border: 1px solid rgba(255, 255, 255, 0.16);
          border-radius: 8px;
          background: rgba(255, 255, 255, 0.045);
          color: #f7f7f2;
          text-decoration: none;
        }
        .market-link:hover, .market-link:focus-visible {
          border-color: rgba(0, 240, 79, 0.82);
          outline: none;
        }
        .market-link strong {
          display: block;
          margin-bottom: 8px;
          color: #00f04f;
          font-size: 13px;
        }
        .market-link span {
          display: -webkit-box;
          overflow: hidden;
          color: #f7f7f2;
          font-size: 15px;
          line-height: 1.3;
          -webkit-box-orient: vertical;
          -webkit-line-clamp: 4;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    links = []
    for row in rows:
        market_id = row.get("id", "")
        href = f"/?market={quote(market_id)}"
        links.append(
            f'<a class="market-link" href="{h(href)}">'
            f"<strong>{h(market_id)}</strong>"
            f"<span>{h(row.get('question', ''))}</span>"
            "</a>"
        )

    st.markdown(
        '<main class="directory">'
        "<h1>Manifold Prediction Cards</h1>"
        "<p>Open a market card for preview. For Manifold embeds, add "
        "<code>&mode=embed&embedded=true</code> to the market URL.</p>"
        f'<div class="market-list">{"".join(links)}</div>'
        "</main>",
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
        [data-testid="stHeader"],
        [data-testid="stToolbar"],
        [data-testid="stDecoration"],
        [data-testid="stStatusWidget"] {
          display: none !important;
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
