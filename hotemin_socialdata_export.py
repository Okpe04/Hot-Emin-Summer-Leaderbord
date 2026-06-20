"""
Export X/Twitter posts about Hot Emin Summer through SocialData API.

The script reads SOCIAL_DATA_KEY from .env or the environment, searches for:
  - @HotEminSummer
  - gHotemin
  - $hotemin

It writes a deduplicated JSON file with normalized tweet records and author
aggregates that can be used by a later website/API.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request


API_BASE_URL = "https://api.socialdata.tools"
DEFAULT_HANDLE = "HotEminSummer"
DEFAULT_TERMS = ("gHotemin", "$hotemin")
DEFAULT_MIN_DATE = "2026-05-13"
RETRYABLE_STATUSES = {429, 500, 502, 503}


class SocialDataError(RuntimeError):
    def __init__(self, status_code: int | None, message: str, payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_date_cutoff(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raw = f"{raw}T00:00:00+00:00"
    elif raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def parse_tweet_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        raw = str(value)
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        return None


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        if key and key not in os.environ:
            os.environ[key] = value


def clean_handle(handle: str) -> str:
    handle = handle.strip()
    if handle.startswith("@"):
        handle = handle[1:]
    if not handle:
        raise ValueError("Twitter handle is empty")
    return handle


def int_or_zero(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def tweet_id_as_int(tweet: dict[str, Any]) -> int | None:
    raw_id = tweet.get("id_str") or tweet.get("id")
    if raw_id is None:
        return None
    try:
        return int(str(raw_id))
    except ValueError:
        return None


def text_from_tweet(tweet: dict[str, Any]) -> str:
    return str(tweet.get("full_text") or tweet.get("text") or "")


def tweet_url(tweet: dict[str, Any]) -> str | None:
    user = tweet.get("user") or {}
    screen_name = user.get("screen_name")
    tweet_id = tweet.get("id_str") or tweet.get("id")
    if not screen_name or not tweet_id:
        return None
    return f"https://x.com/{screen_name}/status/{tweet_id}"


def user_mentions(tweet: dict[str, Any]) -> set[str]:
    entities = tweet.get("entities") or {}
    mentions = entities.get("user_mentions") or []
    result: set[str] = set()
    for mention in mentions:
        screen_name = mention.get("screen_name")
        if screen_name:
            result.add(str(screen_name).lower())
    return result


def cashtags(tweet: dict[str, Any]) -> set[str]:
    entities = tweet.get("entities") or {}
    symbols = entities.get("symbols") or []
    result: set[str] = set()
    for symbol in symbols:
        text = symbol.get("text")
        if text:
            result.add(str(text).lower())
    return result


def detect_matches(tweet: dict[str, Any], handle: str, terms: list[str]) -> list[str]:
    full_text = text_from_tweet(tweet)
    lower_text = full_text.lower()
    handle_lower = handle.lower()
    labels: list[str] = []

    if f"@{handle_lower}" in lower_text or handle_lower in user_mentions(tweet):
        labels.append(f"@{handle}")

    tweet_cashtags = cashtags(tweet)
    for term in terms:
        lower_term = term.lower()
        if lower_term.startswith("$"):
            symbol = lower_term[1:]
            if lower_term in lower_text or symbol in tweet_cashtags:
                labels.append(term)
        elif lower_term in lower_text:
            labels.append(term)

    return labels


def normalize_tweet(tweet: dict[str, Any], handle: str, terms: list[str], include_raw: bool) -> dict[str, Any]:
    user = tweet.get("user") or {}
    normalized = {
        "id": str(tweet.get("id_str") or tweet.get("id") or ""),
        "url": tweet_url(tweet),
        "created_at": tweet.get("tweet_created_at") or tweet.get("created_at"),
        "text": text_from_tweet(tweet),
        "lang": tweet.get("lang"),
        "author": {
            "id": str(user.get("id_str") or user.get("id") or ""),
            "handle": user.get("screen_name"),
            "name": user.get("name"),
            "followers_count": int_or_zero(user.get("followers_count")),
            "profile_image_url": user.get("profile_image_url_https") or user.get("profile_image_url"),
            "verified": bool(user.get("verified")) if user.get("verified") is not None else None,
        },
        "metrics": {
            "likes": int_or_zero(tweet.get("favorite_count")),
            "views": int_or_zero(tweet.get("views_count")),
            "replies": int_or_zero(tweet.get("reply_count")),
            "retweets": int_or_zero(tweet.get("retweet_count")),
            "quotes": int_or_zero(tweet.get("quote_count")),
            "bookmarks": int_or_zero(tweet.get("bookmark_count")),
        },
        "reply_to": {
            "status_id": tweet.get("in_reply_to_status_id_str"),
            "user_id": tweet.get("in_reply_to_user_id_str"),
            "screen_name": tweet.get("in_reply_to_screen_name"),
        },
        "is_quote": bool(tweet.get("is_quote_status")),
        "quoted_status_id": tweet.get("quoted_status_id_str"),
        "matched_terms": detect_matches(tweet, handle, terms),
        "source_queries": [],
    }
    if include_raw:
        normalized["raw"] = tweet
    return normalized


def is_reply_tweet(tweet: dict[str, Any]) -> bool:
    reply_to = tweet.get("reply_to") or {}
    return bool(
        reply_to.get("status_id")
        or reply_to.get("user_id")
        or reply_to.get("screen_name")
        or tweet.get("in_reply_to_status_id_str")
        or tweet.get("in_reply_to_user_id_str")
        or tweet.get("in_reply_to_screen_name")
    )


def is_quote_tweet(tweet: dict[str, Any]) -> bool:
    return bool(tweet.get("is_quote") or tweet.get("quoted_status_id") or tweet.get("is_quote_status"))


def has_required_match(tweet: dict[str, Any]) -> bool:
    return bool(tweet.get("matched_terms"))


def should_keep_tweet(
    tweet: dict[str, Any],
    include_replies: bool,
    include_quotes: bool,
    allow_unmatched: bool,
    min_datetime: datetime | None,
) -> bool:
    if min_datetime is not None:
        tweet_datetime = parse_tweet_datetime(tweet.get("created_at") or tweet.get("tweet_created_at"))
        if tweet_datetime is None or tweet_datetime < min_datetime:
            return False
    if not include_replies and is_reply_tweet(tweet):
        return False
    if not include_quotes and is_quote_tweet(tweet):
        return False
    if not allow_unmatched and not has_required_match(tweet):
        return False
    return True


def merge_unique_values(existing: list[str], incoming: list[str]) -> list[str]:
    merged = list(existing)
    seen = set(existing)
    for item in incoming:
        if item not in seen:
            merged.append(item)
            seen.add(item)
    return merged


def refresh_existing_tweet(existing: dict[str, Any], normalized: dict[str, Any]) -> None:
    for key in ["url", "created_at", "text", "lang", "reply_to", "is_quote", "quoted_status_id"]:
        existing[key] = normalized.get(key)
    existing["author"] = normalized.get("author") or existing.get("author") or {}
    existing["metrics"] = normalized.get("metrics") or existing.get("metrics") or {}


class SocialDataClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = API_BASE_URL,
        timeout: int = 30,
        retries: int = 4,
        backoff_seconds: float = 2.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.backoff_seconds = backoff_seconds

    def search(self, query: str, search_type: str = "Latest", cursor: str | None = None) -> dict[str, Any]:
        params = {"query": query, "type": search_type}
        if cursor:
            params["cursor"] = cursor

        url = f"{self.base_url}/twitter/search?{parse.urlencode(params)}"
        return self._get_json(url)

    def _get_json(self, url: str) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "hotemin-socialdata-export/1.0",
        }

        last_error: SocialDataError | None = None
        for attempt in range(self.retries + 1):
            req = request.Request(url, headers=headers, method="GET")
            try:
                with request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read().decode("utf-8")
                    return json.loads(body) if body else {}
            except error.HTTPError as exc:
                payload = self._read_error_payload(exc)
                message = self._error_message(payload) or f"HTTP {exc.code}"
                last_error = SocialDataError(exc.code, message, payload)

                if exc.code not in RETRYABLE_STATUSES or attempt >= self.retries:
                    raise last_error

                retry_after = exc.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else self.backoff_seconds * (2**attempt)
                time.sleep(delay)
            except (error.URLError, TimeoutError, http.client.RemoteDisconnected, http.client.IncompleteRead, OSError) as exc:
                reason = getattr(exc, "reason", None) or str(exc)
                last_error = SocialDataError(None, reason)
                if attempt >= self.retries:
                    raise last_error
                time.sleep(self.backoff_seconds * (2**attempt))
            except json.JSONDecodeError as exc:
                raise SocialDataError(None, f"Invalid JSON response: {exc}") from exc

        raise last_error or SocialDataError(None, "Unknown SocialData API error")

    @staticmethod
    def _read_error_payload(exc: error.HTTPError) -> Any:
        try:
            raw = exc.read().decode("utf-8")
        except Exception:
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    @staticmethod
    def _error_message(payload: Any) -> str | None:
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("error")
            return str(message) if message else None
        if isinstance(payload, str):
            return payload
        return None


def build_search_specs(
    handle: str,
    terms: list[str],
    extra_operators: list[str],
    exclude_target_author: bool,
) -> list[dict[str, str]]:
    suffix_parts = list(extra_operators)

    mention_parts = [f"@{handle}"]
    if exclude_target_author:
        mention_parts.append(f"-from:{handle}")
    mention_parts.extend(suffix_parts)

    specs = [{"label": f"mention:@{handle}", "query": " ".join(mention_parts)}]
    for term in terms:
        parts = [term]
        if exclude_target_author:
            parts.append(f"-from:{handle}")
        parts.extend(suffix_parts)
        specs.append({"label": f"term:{term}", "query": " ".join(parts)})
    return specs


def make_query_state(spec: dict[str, str]) -> dict[str, Any]:
    return {
        "label": spec["label"],
        "query": spec["query"],
        "search_type": spec.get("search_type", "Latest"),
        "pages": 0,
        "requests": 0,
        "tweets_found": 0,
        "empty_pages": 0,
        "stopped_reason": None,
        "done": False,
        "cursor": None,
        "max_id": None,
        "seen_cursors": [],
        "seen_max_ids": [],
        "used_max_id_fallback": False,
    }


def normalize_query_state(spec: dict[str, str], stored: dict[str, Any] | None) -> dict[str, Any]:
    if (
        not stored
        or stored.get("query") != spec["query"]
        or stored.get("search_type", "Latest") != spec.get("search_type", "Latest")
    ):
        return make_query_state(spec)

    state = make_query_state(spec)
    for key in state:
        if key in {"label", "query", "search_type"}:
            continue
        if key in stored:
            state[key] = stored[key]
    state["seen_cursors"] = [str(item) for item in state.get("seen_cursors") or []]
    state["seen_max_ids"] = [int_or_zero(item) for item in state.get("seen_max_ids") or []]
    state["pages"] = int_or_zero(state.get("pages"))
    state["requests"] = int_or_zero(state.get("requests"))
    state["tweets_found"] = int_or_zero(state.get("tweets_found"))
    state["empty_pages"] = int_or_zero(state.get("empty_pages"))
    state["done"] = bool(state.get("done"))
    state["used_max_id_fallback"] = bool(state.get("used_max_id_fallback"))
    return state


def public_query_stats(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    public: list[dict[str, Any]] = []
    for state in states:
        public.append(
            {
                "label": state["label"],
                "query": state["query"],
                "search_type": state.get("search_type", "Latest"),
                "pages": int_or_zero(state.get("pages")),
                "requests": int_or_zero(state.get("requests")),
                "tweets_found": int_or_zero(state.get("tweets_found")),
                "empty_pages": int_or_zero(state.get("empty_pages")),
                "stopped_reason": state.get("stopped_reason"),
                "done": bool(state.get("done")),
                "used_max_id_fallback": bool(state.get("used_max_id_fallback")),
            }
        )
    return public


def unlock_resumed_limit_states(
    states: list[dict[str, Any]],
    max_pages: int | None,
    max_tweets_per_query: int | None,
) -> None:
    for state in states:
        stopped_reason = state.get("stopped_reason")
        if stopped_reason == "max_pages" and (
            max_pages is None or int_or_zero(state.get("pages")) < max_pages
        ):
            state["done"] = False
            state["stopped_reason"] = None
        if stopped_reason == "max_tweets_per_query" and (
            max_tweets_per_query is None or int_or_zero(state.get("tweets_found")) < max_tweets_per_query
        ):
            state["done"] = False
            state["stopped_reason"] = None


def append_max_id(query: str, max_id: int | None) -> str:
    if max_id is None:
        return query
    return f"{query} max_id:{max_id}"


def collect_query_pages(
    client: SocialDataClient,
    state: dict[str, Any],
    max_pages: int | None,
    max_tweets_per_query: int | None,
    request_sleep: float,
    use_max_id_fallback: bool,
) -> tuple[list[dict[str, Any]], bool]:
    if state.get("done"):
        return [], False

    seen_cursors = set(str(item) for item in state.get("seen_cursors") or [])
    seen_max_ids = set(int_or_zero(item) for item in state.get("seen_max_ids") or [])

    while True:
        if max_pages is not None and int_or_zero(state.get("pages")) >= max_pages:
            state["stopped_reason"] = "max_pages"
            state["done"] = True
            return [], False
        if max_tweets_per_query is not None and int_or_zero(state.get("tweets_found")) >= max_tweets_per_query:
            state["stopped_reason"] = "max_tweets_per_query"
            state["done"] = True
            return [], False

        max_id = state.get("max_id")
        query = append_max_id(state["query"], int(max_id) if max_id is not None else None)
        cursor = state.get("cursor")
        data = client.search(query=query, search_type=state.get("search_type", "Latest"), cursor=cursor)
        state["requests"] = int_or_zero(state.get("requests")) + 1
        state["pages"] = int_or_zero(state.get("pages")) + 1

        page_tweets = data.get("tweets") or []
        if not isinstance(page_tweets, list):
            raise SocialDataError(None, f"Unexpected response shape for query {state['label']}: tweets is not a list", data)

        if not page_tweets:
            state["empty_pages"] = int_or_zero(state.get("empty_pages")) + 1
        state["tweets_found"] = int_or_zero(state.get("tweets_found")) + len(page_tweets)

        if request_sleep > 0:
            time.sleep(request_sleep)

        next_cursor = data.get("next_cursor")
        if next_cursor:
            next_cursor_str = str(next_cursor)
            if next_cursor_str not in seen_cursors:
                seen_cursors.add(next_cursor_str)
                state["seen_cursors"] = sorted(seen_cursors)
                state["cursor"] = next_cursor_str
                return page_tweets, True

        if not use_max_id_fallback:
            state["stopped_reason"] = "completed"
            state["done"] = True
            state["cursor"] = None
            return page_tweets, False

        all_ids = [tweet_id_as_int(tweet) for tweet in page_tweets if isinstance(tweet, dict)]
        numeric_ids = [tweet_id for tweet_id in all_ids if tweet_id is not None]
        if not numeric_ids:
            state["stopped_reason"] = "completed"
            state["done"] = True
            state["cursor"] = None
            return page_tweets, False

        next_max_id = min(numeric_ids) - 1
        if next_max_id <= 0 or next_max_id in seen_max_ids:
            state["stopped_reason"] = "completed"
            state["done"] = True
            state["cursor"] = None
            return page_tweets, False

        seen_max_ids.add(next_max_id)
        state["seen_max_ids"] = sorted(seen_max_ids)
        state["used_max_id_fallback"] = True
        state["max_id"] = next_max_id
        state["cursor"] = None
        return page_tweets, True


def author_summaries(tweets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}

    for tweet in tweets:
        author = tweet.get("author") or {}
        handle = author.get("handle") or ""
        key = str(handle).lower() or str(author.get("id") or "")
        if not key:
            key = "unknown"

        metrics = tweet.get("metrics") or {}
        summary = summaries.setdefault(
            key,
            {
                "author": author,
                "tweet_count": 0,
                "likes": 0,
                "views": 0,
                "replies": 0,
                "retweets": 0,
                "quotes": 0,
                "bookmarks": 0,
                "matched_terms": [],
                "tweet_ids": [],
                "first_tweet_at": None,
                "last_tweet_at": None,
            },
        )

        summary["tweet_count"] += 1
        for metric in ("likes", "views", "replies", "retweets", "quotes", "bookmarks"):
            summary[metric] += int_or_zero(metrics.get(metric))

        summary["matched_terms"] = merge_unique_values(summary["matched_terms"], tweet.get("matched_terms") or [])
        if tweet.get("id"):
            summary["tweet_ids"].append(tweet["id"])

        created_at = tweet.get("created_at")
        if created_at:
            if summary["first_tweet_at"] is None or created_at < summary["first_tweet_at"]:
                summary["first_tweet_at"] = created_at
            if summary["last_tweet_at"] is None or created_at > summary["last_tweet_at"]:
                summary["last_tweet_at"] = created_at

    return sorted(
        summaries.values(),
        key=lambda item: (item["tweet_count"], item["likes"], item["views"]),
        reverse=True,
    )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_resume_data(path: Path, specs: list[dict[str, str]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    if not path.exists():
        return {}, [make_query_state(spec) for spec in specs]

    payload = json.loads(path.read_text(encoding="utf-8"))
    tweets_by_id: dict[str, dict[str, Any]] = {}
    for tweet in payload.get("tweets") or []:
        if not isinstance(tweet, dict):
            continue
        tweet_id = str(tweet.get("id") or "")
        if tweet_id:
            tweets_by_id[tweet_id] = tweet

    collection = payload.get("collection") or {}
    stored_states = collection.get("resume_state") or []
    states_by_label = {
        str(state.get("label")): state for state in stored_states if isinstance(state, dict) and state.get("label")
    }
    states = []
    for spec in specs:
        stored = states_by_label.get(spec["label"])
        if stored is None and spec.get("search_type") == "Latest" and spec["label"].startswith("Latest:"):
            stored = states_by_label.get(spec["label"].removeprefix("Latest:"))
        states.append(normalize_query_state(spec, stored))
    return tweets_by_id, states


def build_output_payload(
    handle: str,
    terms: list[str],
    constraints: list[str],
    search_type: str,
    exclude_target_author: bool,
    states: list[dict[str, Any]],
    tweets_by_id: dict[str, dict[str, Any]],
    status: str,
) -> dict[str, Any]:
    tweets = sorted(
        tweets_by_id.values(),
        key=lambda tweet: int_or_zero(tweet.get("id")),
        reverse=True,
    )
    users = author_summaries(tweets)

    totals = defaultdict(int)
    for tweet in tweets:
        for metric, value in (tweet.get("metrics") or {}).items():
            totals[metric] += int_or_zero(value)

    return {
        "generated_at": utc_now_iso(),
        "source": {
            "provider": "SocialData",
            "api_base_url": API_BASE_URL,
            "docs": "https://docs.socialdata.tools/",
        },
        "project": {
            "handle": handle,
            "terms": terms,
        },
        "collection": {
            "status": status,
            "search_type": search_type,
            "constraints": constraints,
            "exclude_target_author": bool(exclude_target_author),
            "tweet_count": len(tweets),
            "user_count": len(users),
            "queries": public_query_stats(states),
            "resume_state": states,
            "metric_totals": dict(totals),
        },
        "users": users,
        "tweets": tweets,
    }


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export Hot Emin Summer related X/Twitter posts through SocialData API to JSON.",
    )
    parser.add_argument("--handle", default=DEFAULT_HANDLE, help="Project Twitter/X handle without @.")
    parser.add_argument(
        "--term",
        dest="terms",
        action="append",
        help="Additional term to search. Can be passed multiple times. Defaults to gHotemin and $hotemin.",
    )
    parser.add_argument("--output", default="data/hotemin_posts.json", help="JSON output path.")
    parser.add_argument("--env", default=".env", help="Path to .env file with SOCIAL_DATA_KEY.")
    parser.add_argument(
        "--type",
        default="Both",
        choices=["Latest", "Top", "Both"],
        help="SocialData search type. Both collects Latest and Top and deduplicates tweet ids.",
    )
    parser.add_argument("--since", help="Twitter search since date, for example 2026-01-01.")
    parser.add_argument("--until", help="Twitter search until date, for example 2026-06-19.")
    parser.add_argument(
        "--min-date",
        default=DEFAULT_MIN_DATE,
        help="Local minimum tweet date to keep, inclusive. Defaults to 2026-05-13. Use --min-date none to disable.",
    )
    parser.add_argument("--since-time", type=positive_int, help="Twitter search since_time unix timestamp.")
    parser.add_argument("--until-time", type=positive_int, help="Twitter search until_time unix timestamp.")
    parser.add_argument("--extra-query", action="append", default=[], help="Extra Twitter search operator.")
    parser.add_argument("--max-pages", type=positive_int, help="Maximum API pages per search query.")
    parser.add_argument("--max-tweets-per-query", type=positive_int, help="Maximum tweets to keep per search query.")
    parser.add_argument("--sleep", type=non_negative_float, default=0.6, help="Delay between requests in seconds.")
    parser.add_argument("--timeout", type=positive_int, default=30, help="HTTP timeout in seconds.")
    parser.add_argument("--retries", type=int, default=4, help="Retries for 429/5xx/network errors.")
    parser.add_argument("--no-max-id-fallback", action="store_true", help="Disable max_id fallback after cursor ends.")
    parser.add_argument("--exclude-target-author", action="store_true", help="Add -from:HANDLE to all queries.")
    parser.add_argument("--include-replies", action="store_true", help="Keep reply tweets. By default replies are skipped.")
    parser.add_argument("--include-quotes", action="store_true", help="Keep quote tweets. By default quotes are skipped.")
    parser.add_argument(
        "--allow-unmatched",
        action="store_true",
        help="Keep tweets even if no required term was detected locally. By default these are skipped.",
    )
    parser.add_argument("--include-raw", action="store_true", help="Include original SocialData tweet objects.")
    parser.add_argument("--resume", action="store_true", help="Resume from an existing output JSON checkpoint.")
    parser.add_argument("--clean-only", action="store_true", help="Filter the existing output JSON without calling the API.")
    parser.add_argument(
        "--refresh-latest-pages",
        type=positive_int,
        help="Fetch the first N newest pages for each query, merge new tweets, and refresh metrics without moving deep resume cursors.",
    )
    parser.add_argument("--checkpoint-every", type=positive_int, default=1, help="Write output JSON every N pages.")
    parser.add_argument("--log-file", help="Optional path to append progress logs, for example data/hotemin_collect.log.")
    parser.add_argument("--quiet", action="store_true", help="Disable per-page progress output.")
    parser.add_argument("--dry-run", action="store_true", help="Print queries and exit without calling API.")
    return parser


def query_constraints(args: argparse.Namespace) -> list[str]:
    constraints: list[str] = []
    if args.since:
        constraints.append(f"since:{args.since}")
    if args.until:
        constraints.append(f"until:{args.until}")
    if args.since_time:
        constraints.append(f"since_time:{args.since_time}")
    if args.until_time:
        constraints.append(f"until_time:{args.until_time}")
    constraints.extend(args.extra_query or [])
    return constraints


def expand_specs_for_search_types(specs: list[dict[str, str]], search_types: list[str]) -> list[dict[str, str]]:
    expanded: list[dict[str, str]] = []
    for search_type in search_types:
        for spec in specs:
            expanded.append(
                {
                    "label": f"{search_type}:{spec['label']}",
                    "query": spec["query"],
                    "search_type": search_type,
                }
            )
    return expanded


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    handle = clean_handle(args.handle)
    terms = args.terms if args.terms else list(DEFAULT_TERMS)
    constraints = query_constraints(args)
    base_specs = build_search_specs(
        handle=handle,
        terms=terms,
        extra_operators=constraints,
        exclude_target_author=args.exclude_target_author,
    )
    search_types = ["Latest", "Top"] if args.type == "Both" else [args.type]
    specs = expand_specs_for_search_types(base_specs, search_types)

    if args.dry_run:
        for spec in specs:
            print(f"{spec['label']}: {spec['query']}")
        return 0

    load_env_file(Path(args.env))
    api_key = os.environ.get("SOCIAL_DATA_KEY")
    if not api_key:
        print("SOCIAL_DATA_KEY is not set. Add it to .env or the environment.", file=sys.stderr)
        return 2

    if args.retries < 0:
        print("--retries must be zero or greater.", file=sys.stderr)
        return 2

    min_datetime = None if str(args.min_date).lower() == "none" else parse_date_cutoff(args.min_date)
    client = SocialDataClient(api_key=api_key, timeout=args.timeout, retries=args.retries)
    output_path = Path(args.output)

    if args.resume:
        tweets_by_id, query_states = load_resume_data(output_path, specs)
        unlock_resumed_limit_states(query_states, args.max_pages, args.max_tweets_per_query)
    else:
        tweets_by_id = {}
        query_states = [make_query_state(spec) for spec in specs]

    existing_before_filter = len(tweets_by_id)
    tweets_by_id = {
        tweet_id: tweet
        for tweet_id, tweet in tweets_by_id.items()
        if should_keep_tweet(
            tweet,
            include_replies=args.include_replies,
            include_quotes=args.include_quotes,
            allow_unmatched=args.allow_unmatched,
            min_datetime=min_datetime,
        )
    }

    pages_since_checkpoint = 0
    log_path = Path(args.log_file) if args.log_file else None

    def log(message: str, *, force: bool = False) -> None:
        line = f"{utc_now_iso()} {message}"
        if force or not args.quiet:
            print(line, flush=True)
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(line + "\n")

    def checkpoint(status: str) -> None:
        payload = build_output_payload(
            handle=handle,
            terms=terms,
            constraints=constraints,
            search_type=",".join(search_types),
            exclude_target_author=args.exclude_target_author,
            states=query_states,
            tweets_by_id=tweets_by_id,
            status=status,
        )
        write_json(output_path, payload)
        log(
            f"[checkpoint] status={status} file={output_path} "
            f"unique_tweets={payload['collection']['tweet_count']} users={payload['collection']['user_count']}"
        )

    def merge_page(raw_tweets: list[dict[str, Any]], source_label: str) -> dict[str, int]:
        before_count = len(tweets_by_id)
        skipped_replies = 0
        skipped_quotes = 0
        skipped_unmatched = 0
        skipped_too_old = 0
        refreshed_existing = 0
        for raw_tweet in raw_tweets:
            if not isinstance(raw_tweet, dict):
                continue
            normalized = normalize_tweet(raw_tweet, handle, terms, args.include_raw)
            tweet_datetime = parse_tweet_datetime(normalized.get("created_at"))
            if min_datetime is not None and (tweet_datetime is None or tweet_datetime < min_datetime):
                skipped_too_old += 1
                continue
            if not args.include_replies and is_reply_tweet(normalized):
                skipped_replies += 1
                continue
            if not args.include_quotes and is_quote_tweet(normalized):
                skipped_quotes += 1
                continue
            if not args.allow_unmatched and not has_required_match(normalized):
                skipped_unmatched += 1
                continue

            tweet_id = normalized["id"]
            if not tweet_id:
                continue

            if tweet_id not in tweets_by_id:
                tweets_by_id[tweet_id] = normalized
            else:
                refresh_existing_tweet(tweets_by_id[tweet_id], normalized)
                refreshed_existing += 1

            existing = tweets_by_id[tweet_id]
            existing["source_queries"] = merge_unique_values(existing["source_queries"], [source_label])
            existing["matched_terms"] = merge_unique_values(
                existing.get("matched_terms") or [],
                normalized.get("matched_terms") or [],
            )
        return {
            "new_unique": len(tweets_by_id) - before_count,
            "skipped_replies": skipped_replies,
            "skipped_quotes": skipped_quotes,
            "skipped_unmatched": skipped_unmatched,
            "skipped_too_old": skipped_too_old,
            "refreshed_existing": refreshed_existing,
        }

    log(
        f"[start] mode={'resume' if args.resume else 'fresh'} output={output_path} "
        f"search_type={','.join(search_types)} existing_unique_tweets={len(tweets_by_id)} "
        f"removed_existing={existing_before_filter - len(tweets_by_id)} "
        f"min_date={args.min_date} "
        f"include_replies={args.include_replies} include_quotes={args.include_quotes} "
        f"allow_unmatched={args.allow_unmatched} "
        f"queries={len(query_states)}"
    )
    if args.clean_only:
        checkpoint("partial")
        print(
            f"Cleaned {output_path}: kept {len(tweets_by_id)} tweets, "
            f"removed {existing_before_filter - len(tweets_by_id)} tweets."
        )
        return 0

    if args.refresh_latest_pages:
        refresh_added = 0
        refresh_updated = 0
        refresh_requests = 0
        try:
            for spec in specs:
                refresh_state = make_query_state(spec)
                log(
                    f"[refresh-start] query={refresh_state['label']} "
                    f"search_type={refresh_state.get('search_type')} pages={args.refresh_latest_pages}"
                )
                while not refresh_state.get("done"):
                    pages_before = int_or_zero(refresh_state.get("pages"))
                    raw_tweets, should_continue = collect_query_pages(
                        client=client,
                        state=refresh_state,
                        max_pages=args.refresh_latest_pages,
                        max_tweets_per_query=None,
                        request_sleep=args.sleep,
                        use_max_id_fallback=False,
                    )
                    made_request = int_or_zero(refresh_state.get("pages")) > pages_before
                    merge_stats = merge_page(raw_tweets, refresh_state["label"])
                    refresh_added += merge_stats["new_unique"]
                    refresh_updated += merge_stats["refreshed_existing"]
                    if made_request:
                        refresh_requests += 1
                        log(
                            f"[refresh-page] query={refresh_state['label']} page={refresh_state['pages']} "
                            f"page_tweets={len(raw_tweets)} new_unique={merge_stats['new_unique']} "
                            f"refreshed_existing={merge_stats['refreshed_existing']} "
                            f"skipped_replies={merge_stats['skipped_replies']} "
                            f"skipped_quotes={merge_stats['skipped_quotes']} "
                            f"skipped_unmatched={merge_stats['skipped_unmatched']} "
                            f"skipped_too_old={merge_stats['skipped_too_old']} "
                            f"unique_total={len(tweets_by_id)}"
                        )
                    if not should_continue:
                        break
        except KeyboardInterrupt:
            checkpoint("interrupted")
            log(f"[interrupted] checkpoint saved to {output_path}. Resume with --resume.", force=True)
            return 130
        except SocialDataError as exc:
            checkpoint("error")
            status = f"HTTP {exc.status_code}: " if exc.status_code is not None else ""
            print(f"SocialData API error: {status}{exc}", file=sys.stderr)
            print(f"Checkpoint saved to {output_path}. Resume with --resume.", file=sys.stderr)
            return 1

        checkpoint("partial")
        print(
            f"Refreshed latest pages: {refresh_requests} requests, "
            f"{refresh_added} new tweets, {refresh_updated} existing tweets refreshed. "
            f"Wrote {len(tweets_by_id)} unique tweets to {output_path}."
        )
        return 0

    try:
        for state in query_states:
            if state.get("done"):
                log(
                    f"[skip] query={state['label']} pages={state['pages']} "
                    f"tweets_found={state['tweets_found']} stopped={state.get('stopped_reason')}"
                )
                continue

            log(
                f"[query-start] query={state['label']} search_type={state.get('search_type')} "
                f"pages_done={state['pages']} tweets_found={state['tweets_found']} cursor_saved={bool(state.get('cursor'))}"
            )
            while not state.get("done"):
                pages_before = int_or_zero(state.get("pages"))
                raw_tweets, should_continue = collect_query_pages(
                    client=client,
                    state=state,
                    max_pages=args.max_pages,
                    max_tweets_per_query=args.max_tweets_per_query,
                    request_sleep=args.sleep,
                    use_max_id_fallback=not args.no_max_id_fallback,
                )
                made_request = int_or_zero(state.get("pages")) > pages_before
                merge_stats = merge_page(raw_tweets, state["label"])
                if made_request:
                    pages_since_checkpoint += 1

                if made_request:
                    log(
                        f"[page] query={state['label']} page={state['pages']} "
                        f"page_tweets={len(raw_tweets)} new_unique={merge_stats['new_unique']} "
                        f"refreshed_existing={merge_stats['refreshed_existing']} "
                        f"skipped_replies={merge_stats['skipped_replies']} "
                        f"skipped_quotes={merge_stats['skipped_quotes']} "
                        f"skipped_unmatched={merge_stats['skipped_unmatched']} "
                        f"skipped_too_old={merge_stats['skipped_too_old']} "
                        f"unique_total={len(tweets_by_id)} requests={state['requests']} "
                        f"done={state['done']} stopped={state.get('stopped_reason') or '-'}"
                    )

                if pages_since_checkpoint >= args.checkpoint_every or state.get("done"):
                    checkpoint("running")
                    pages_since_checkpoint = 0

                if not should_continue:
                    break
            log(
                f"[query-end] query={state['label']} pages={state['pages']} "
                f"tweets_found={state['tweets_found']} requests={state['requests']} "
                f"stopped={state.get('stopped_reason')}"
            )
    except KeyboardInterrupt:
        checkpoint("interrupted")
        log(f"[interrupted] checkpoint saved to {output_path}. Resume with --resume.", force=True)
        return 130
    except SocialDataError as exc:
        checkpoint("error")
        status = f"HTTP {exc.status_code}: " if exc.status_code is not None else ""
        print(f"SocialData API error: {status}{exc}", file=sys.stderr)
        print(f"Checkpoint saved to {output_path}. Resume with --resume.", file=sys.stderr)
        return 1

    fully_completed = all(
        bool(state.get("done")) and state.get("stopped_reason") == "completed" for state in query_states
    )
    output_status = "complete" if fully_completed else "partial"
    checkpoint(output_status)

    payload = build_output_payload(
        handle=handle,
        terms=terms,
        constraints=constraints,
        search_type=",".join(search_types),
        exclude_target_author=args.exclude_target_author,
        states=query_states,
        tweets_by_id=tweets_by_id,
        status=output_status,
    )
    stats = payload["collection"]["queries"]

    print(
        f"Wrote {payload['collection']['tweet_count']} unique tweets from "
        f"{payload['collection']['user_count']} users to {output_path} "
        f"(status={output_status})"
    )
    for stat in stats:
        print(
            f"- {stat['label']}: {stat['tweets_found']} tweets, "
            f"{stat['requests']} requests, stopped={stat['stopped_reason']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
