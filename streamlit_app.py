from __future__ import annotations

import csv
import html
import os
import re
from urllib.parse import quote
from urllib.parse import urlsplit

import streamlit as st
import streamlit.components.v1 as components


PREFERRED_INPUTS = [
    os.path.join("Markets", "MarketNewsPredictions.csv"),
    os.path.join("Markets", "MarketNewsPredictions.10_sample.csv"),
    os.path.join("Markets", "MarketNewsPredictions.sample.csv"),
]


def h(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def parse_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def pct(value: object) -> str:
    return f"{round(parse_float(value) * 100)}%"


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
    for index, url in enumerate(urls[:3], start=1):
        links.append(
            f'<a class="source-pill" href="{h(url)}" target="_blank" '
            f'rel="noopener noreferrer" title="{h(url)}">'
            f"Source {index}: {h(source_label(url))}</a>"
        )
    return "\n".join(links)


def card_html(row: dict[str, str]) -> str:
    yes = parse_float(row.get("newsPredictedYesProbability"))
    no = parse_float(row.get("newsPredictedNoProbability"), 1 - yes)
    yes_score_class = "higher-score" if yes >= no else "lower-score"
    no_score_class = "higher-score" if no > yes else "lower-score"
    band = probability_band(yes)
    question = row.get("question", "").strip()
    confidence = (row.get("newsConfidence", "").strip() or "unknown").upper()
    source_count = len(split_pipe(row.get("newsSourceUrls", "")))
    model = row.get("forecastModel", "").strip()
    forecast_date = row.get("forecastCurrentDate", "").strip()
    reason = row.get("newsShortReason", "").strip()
    market_url = row.get("url", "").strip()
    symbol = market_symbol(question)

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
  aspect-ratio: 16 / 9;
  min-height: 540px;
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
  top: 49%;
  transform: translate(-50%, -50%) rotate(10deg);
  color: rgba(255, 255, 255, 0.13);
  font-size: min(31vw, 430px);
  font-weight: 900;
  line-height: 1;
  z-index: -1;
  user-select: none;
}}
.poster-header {{
  position: absolute;
  left: 4.5%;
  top: 3.5%;
  width: min(78%, 1080px);
}}
.poster-header h1 {{
  display: -webkit-box;
  margin: 0;
  overflow: hidden;
  color: var(--ink);
  font-size: min(3vw, 38px);
  font-weight: 800;
  line-height: 1.22;
  text-shadow: 0 3px 0 rgba(0, 0, 0, 0.65);
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}}
.poster-meta {{
  display: flex;
  flex-wrap: wrap;
  gap: 34px;
  margin-top: 22px;
  color: var(--muted);
  font-size: min(2.2vw, 36px);
  line-height: 1.15;
  text-shadow: 0 2px 0 rgba(0, 0, 0, 0.8);
}}
.poster-odds {{
  position: absolute;
  left: 21%;
  right: 21%;
  top: 40%;
  display: grid;
  grid-template-columns: minmax(180px, 1fr) 1px minmax(180px, 1fr);
  align-items: center;
  gap: 6%;
}}
.split-line {{
  width: 1px;
  height: 190px;
  background: rgba(255, 255, 255, 0.28);
}}
.odds-side {{ display: grid; gap: 34px; }}
.no-side {{ text-align: right; }}
.outcome-label {{
  color: var(--ink);
  font-size: min(3.1vw, 58px);
  font-weight: 700;
  line-height: 1;
  text-shadow: 0 3px 0 rgba(0, 0, 0, 0.8);
}}
.odds-side strong {{
  font-size: min(7.2vw, 130px);
  font-weight: 900;
  line-height: 0.95;
  text-shadow: 0 6px 0 rgba(0, 0, 0, 0.78);
}}
.odds-side strong.higher-score {{ color: var(--yes); }}
.odds-side strong.lower-score {{ color: var(--no); }}
.poster-footer {{
  position: absolute;
  left: 4.5%;
  right: 4.5%;
  bottom: 4.4%;
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 30px;
  align-items: end;
}}
.poster-date {{
  display: block;
  color: var(--ink);
  font-size: min(2.4vw, 44px);
  line-height: 1;
  text-shadow: 0 2px 0 rgba(0, 0, 0, 0.75);
}}
.poster-reason {{
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
}}
.poster-reason span, .source-caption {{ color: var(--muted); }}
.poster-sources {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
.source-caption {{ font-size: 13px; line-height: 1; }}
.source-pill {{
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
  width: min(11.5vw, 134px);
  min-width: 78px;
  aspect-ratio: 1 / 0.76;
  place-items: center;
  border: 1px solid rgba(255, 255, 255, 0.23);
  border-radius: 20px;
  background: rgba(255, 255, 255, 0.045);
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
@media (max-width: 900px) {{
  .forecast-poster {{ min-height: 430px; }}
  .poster-header h1 {{ font-size: 30px; }}
  .poster-meta {{ gap: 18px; font-size: 22px; }}
  .poster-odds {{ top: 38%; left: 15%; right: 15%; }}
  .outcome-label {{ font-size: 34px; }}
  .odds-side strong {{ font-size: 70px; }}
  .poster-date {{ font-size: 28px; }}
  .poster-reason {{ font-size: 16px; }}
}}
</style>
</head>
<body>
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
      <span class="outcome-label">YES</span>
      <strong class="{yes_score_class}">{h(pct(yes))}</strong>
    </div>
    <div class="split-line" aria-hidden="true"></div>
    <div class="odds-side no-side">
      <span class="outcome-label">NO</span>
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


def selected_market_id() -> str:
    return st.query_params.get("market") or st.query_params.get("id") or ""


def selected_market(rows: list[dict[str, str]], market_id: str) -> dict[str, str] | None:
    for row in rows:
        if row.get("id") == market_id:
            return row
    return None


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
        href = f"?market={quote(market_id)}"
        links.append(
            f'<a class="market-link" href="{h(href)}">'
            f"<strong>{h(market_id)}</strong>"
            f"<span>{h(row.get('question', ''))}</span>"
            "</a>"
        )

    st.markdown(
        '<main class="directory">'
        "<h1>Manifold Prediction Cards</h1>"
        "<p>Open a market card, then use that full streamlit.app URL in Manifold.</p>"
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
        header, footer, [data-testid="stToolbar"], [data-testid="stDecoration"] {
          display: none !important;
        }
        .block-container {
          padding: 0 !important;
          max-width: none !important;
        }
        iframe {
          display: block;
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

    components.html(card_html(row), height=720, scrolling=False)

    if st.query_params.get("list") == "1":
        st.write("Available market ids:")
        for item in rows:
            st.write(f"- `{item.get('id')}`: {item.get('question')}")


if __name__ == "__main__":
    main()
