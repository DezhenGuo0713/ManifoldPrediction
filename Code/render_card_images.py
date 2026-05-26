#!/usr/bin/env python3
"""Render generated market card pages to PNG images.

The Manifold native app can fail to render third-party iframes inside its
WebView. Static images are a safer fallback for app users, so this script
captures each generated card page as docs/cards/<market-id>.png.
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import subprocess
import sys
from pathlib import Path

from build_prediction_pages import DOCS_DIR, PREFERRED_INPUTS, load_rows, resolve_input, slugify


PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_WIDTH = 1200
DEFAULT_HEIGHT = 675


CHROME_CANDIDATES = [
    os.environ.get("CHROME_BIN", ""),
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]


def find_browser() -> str:
    for candidate in CHROME_CANDIDATES:
        if not candidate:
            continue
        if os.path.isabs(candidate) and os.path.exists(candidate):
            return candidate
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise RuntimeError(
        "No Chrome/Chromium executable found. Set CHROME_BIN to render images."
    )


def file_url(path: Path) -> str:
    return path.resolve().as_uri()


def render_card(
    browser: str,
    page_path: Path,
    output_path: Path,
    width: int,
    height: int,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--hide-scrollbars",
        f"--window-size={width},{height}",
        f"--screenshot={output_path}",
        file_url(page_path),
    ]
    subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def render_images(input_csv: str, output_dir: str, width: int, height: int) -> int:
    rows = load_rows(input_csv)
    browser = find_browser()
    count = 0
    for row in rows:
        market_id = slugify(row["id"])
        page_path = Path(output_dir) / "markets" / market_id / "index.html"
        if not page_path.exists():
            raise FileNotFoundError(f"Missing generated card page: {page_path}")
        output_path = Path(output_dir) / "cards" / f"{market_id}.png"
        render_card(browser, page_path, output_path, width, height)
        count += 1
    return count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render generated market cards to static PNG images."
    )
    parser.add_argument(
        "--input",
        default=None,
        help="Prediction CSV. Defaults to MarketNewsPredictions.csv, then samples.",
    )
    parser.add_argument(
        "--output-dir",
        default=DOCS_DIR,
        help=f"Generated site directory. Default: {DOCS_DIR}",
    )
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_csv = resolve_input(args.input)
    count = render_images(input_csv, args.output_dir, args.width, args.height)
    print(f"Rendered {count} card images.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
