#!/usr/bin/env python3
"""Post a generated forecast-card embed to a Manifold market comment.

The script derives the Streamlit embed/card URL from the Manifold market id:

    https://manifoldprediction.streamlit.app/~/+/?market=<market-id>&mode=embed&embedded=true

It accepts either a full Manifold market URL, a market slug, or a market id. By
default it runs as a dry run. Pass --post to write the comment using
MANIFOLD_API_KEY. The default post format is TipTap JSON with an iframe node,
which Manifold renders as an embedded card.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen


MANIFOLD_API_BASE = "https://api.manifold.markets/v0"
DEFAULT_SITE_BASE_URL = os.environ.get(
    "PREDICTION_STREAMLIT_BASE_URL",
    "https://manifoldprediction.streamlit.app",
).rstrip("/")
DEFAULT_STREAMLIT_DIRECT_PATH = os.environ.get("STREAMLIT_DIRECT_PATH", "/~/+/")
USER_AGENT = "manifold-embed-comment-poster/1.0"


def fetch_json(
    path: str,
    params: dict[str, Any] | None = None,
    api_key: str | None = None,
) -> Any:
    query = urlencode(params or {})
    url = f"{MANIFOLD_API_BASE}{path}"
    if query:
        url = f"{url}?{query}"

    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if api_key:
        headers["Authorization"] = f"Key {api_key}"

    request = Request(url, headers=headers)
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def post_json(path: str, payload: dict[str, Any], api_key: str) -> Any:
    request = Request(
        f"{MANIFOLD_API_BASE}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": f"Key {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        try:
            body = error.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise RuntimeError(f"Manifold HTTP {error.code}: {body[:1000]}") from error


def market_ref_from_value(value: str) -> str:
    text = value.strip()
    parsed = urlparse(text)
    if parsed.netloc.endswith("manifold.markets"):
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            raise ValueError(f"Could not find market slug in URL: {value}")
        return parts[-1]
    return text


def looks_like_market_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{6,}", value.strip()))


def fetch_market(market_ref: str) -> dict[str, Any]:
    ref = market_ref_from_value(market_ref)
    endpoints = [f"/slug/{quote(ref)}"]
    if looks_like_market_id(ref):
        endpoints.append(f"/market/{quote(ref)}")

    last_error: Exception | None = None
    for endpoint in endpoints:
        try:
            result = fetch_json(endpoint)
        except Exception as error:
            last_error = error
            continue
        if isinstance(result, dict) and result.get("id"):
            return result

    if last_error:
        raise RuntimeError(f"Could not fetch market {market_ref}: {last_error}")
    raise RuntimeError(f"Could not fetch market {market_ref}")


def generated_market_url(
    market_id: str,
    site_base_url: str,
    streamlit_path: str,
) -> str:
    base = site_base_url.rstrip("/")
    path = streamlit_path if streamlit_path.startswith("/") else f"/{streamlit_path}"
    return (
        f"{base}{path}?market={quote(market_id)}"
        "&mode=embed&embedded=true"
    )


def comment_markdown(embed_url: str, prefix: str) -> str:
    prefix = prefix.strip()
    if prefix:
        return f"{prefix}\n\n{embed_url}"
    return embed_url


def iframe_comment_content(embed_url: str, prefix: str) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if prefix.strip():
        content.append(
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": prefix.strip()}],
            }
        )
    content.append(
        {
            "type": "iframe",
            "attrs": {
                "src": embed_url,
                "frameBorder": 0,
            },
        }
    )
    return {
        "type": "doc",
        "content": content,
    }


def normalize_comments_response(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    if isinstance(response, dict) and isinstance(response.get("comments"), list):
        return [item for item in response["comments"] if isinstance(item, dict)]
    if isinstance(response, dict) and isinstance(response.get("value"), list):
        return [item for item in response["value"] if isinstance(item, dict)]
    return []


def comment_text(comment: dict[str, Any]) -> str:
    pieces: list[str] = []
    for key in ("content", "markdown", "html", "text"):
        value = comment.get(key)
        if isinstance(value, str):
            pieces.append(value)
    return "\n".join(pieces)


def content_has_iframe_src(node: Any, embed_url: str) -> bool:
    if isinstance(node, list):
        return any(content_has_iframe_src(item, embed_url) for item in node)
    if not isinstance(node, dict):
        return False

    attrs = node.get("attrs")
    if (
        node.get("type") == "iframe"
        and isinstance(attrs, dict)
        and attrs.get("src") == embed_url
    ):
        return True

    return content_has_iframe_src(node.get("content"), embed_url)


def existing_comment_has_url(
    contract_id: str,
    embed_url: str,
    require_iframe: bool,
) -> bool:
    response = fetch_json("/comments", {"contractId": contract_id, "limit": 100})
    comments = normalize_comments_response(response)
    if require_iframe:
        return any(
            content_has_iframe_src(comment.get("content"), embed_url)
            for comment in comments
        )
    return any(embed_url in comment_text(comment) for comment in comments)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Post the generated forecast-card URL to a Manifold market comment."
    )
    parser.add_argument(
        "market",
        help="Manifold market URL, slug, or id.",
    )
    parser.add_argument(
        "--site-base-url",
        default=DEFAULT_SITE_BASE_URL,
        help=f"Streamlit app base URL. Default: {DEFAULT_SITE_BASE_URL}",
    )
    parser.add_argument(
        "--streamlit-path",
        default=DEFAULT_STREAMLIT_DIRECT_PATH,
        help=f"Streamlit direct path. Default: {DEFAULT_STREAMLIT_DIRECT_PATH}",
    )
    parser.add_argument(
        "--prefix",
        default="AI forecast card:",
        help="Text placed before the embed URL. Use an empty string for URL-only comments.",
    )
    parser.add_argument(
        "--post",
        action="store_true",
        help="Actually post the comment. Without this flag, only print a dry run.",
    )
    parser.add_argument(
        "--markdown-link",
        action="store_true",
        help="Post a markdown link instead of a TipTap iframe embed.",
    )
    parser.add_argument(
        "--allow-duplicate",
        action="store_true",
        help="Post even if the same generated URL already appears in recent comments.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    market = fetch_market(args.market)
    contract_id = str(market["id"])
    embed_url = generated_market_url(
        contract_id,
        args.site_base_url,
        args.streamlit_path,
    )
    markdown = comment_markdown(embed_url, args.prefix)
    content = iframe_comment_content(embed_url, args.prefix)

    summary = {
        "marketId": contract_id,
        "marketSlug": market.get("slug", ""),
        "marketQuestion": market.get("question", ""),
        "marketUrl": market.get("url", ""),
        "embedUrl": embed_url,
        "commentMarkdown": markdown,
        "commentContent": content,
        "format": "markdown" if args.markdown_link else "iframe",
        "post": bool(args.post),
    }

    if not args.allow_duplicate and existing_comment_has_url(
        contract_id,
        embed_url,
        require_iframe=not args.markdown_link,
    ):
        summary["status"] = "skipped_duplicate"
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    if not args.post:
        summary["status"] = "dry_run"
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    api_key = os.environ.get("MANIFOLD_API_KEY")
    if not api_key:
        print(
            "MANIFOLD_API_KEY is required to post. Re-run without --post for a dry run.",
            file=sys.stderr,
        )
        return 2

    payload = (
        {"contractId": contract_id, "markdown": markdown}
        if args.markdown_link
        else {"contractId": contract_id, "content": content}
    )
    result = post_json("/comment", payload, api_key)
    summary["status"] = "posted"
    summary["response"] = result
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
