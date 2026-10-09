"""Run a targeted, incremental YouTube discovery experiment.

Run from the repository root:
    python scripts/youtube_expanded_discovery_test.py

This preserves the baseline CSVs and writes separate expanded-experiment outputs.
It uses the official YouTube Data API and never invents missing profile data.
Search terms are discovery hints; all content relevance still requires review.
"""
import csv
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "feasibility"
BASELINE_CHANNELS = DATA_DIR / "youtube_candidates.csv"
BASELINE_VIDEOS = DATA_DIR / "youtube_recent_videos.csv"
OUTPUT_CHANNELS = DATA_DIR / "youtube_expanded_candidates.csv"
OUTPUT_VIDEOS = DATA_DIR / "youtube_expanded_recent_videos.csv"
OUTPUT_SUMMARY = DATA_DIR / "youtube_expanded_summary.csv"
OUTPUT_JSON = DATA_DIR / "youtube_expanded_run_summary.json"

load_dotenv(ROOT / ".env")
API_KEY = os.getenv("YOUTUBE_API_KEY")
BASE_URL = "https://www.googleapis.com/youtube/v3"

# Ten focused queries = ten search.list calls. YouTube Data API search calls
# consume quota, so this is intentionally a bounded experiment, not a crawler.
SEARCH_TERMS = [
    "AI productivity tools tutorial",
    "ChatGPT Claude Gemini tool review",
    "AI tools for content creators",
    "generative AI workflow tutorial",
    "LLM developer tools tutorial",
    "Python coding projects tutorial",
    "web development tools review",
    "machine learning projects tutorial",
    "developer productivity tools review",
    "laptop smartphone tech review",
]
RESULTS_PER_SEARCH = 25
MIN_SUBSCRIBERS = 5_000
MAX_SUBSCRIBERS = 100_000
EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])",
    re.I,
)
INSTAGRAM_RE = re.compile(
    r"""https?://(?:www\.)?instagram\.com/[^\s<>"'|,]+""", re.I
)


def read_csv(path):
    if not path.exists():
        raise FileNotFoundError(
            f"Required baseline file not found: {path}. "
            "Run youtube_discovery_test.py first."
        )
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def youtube_get(endpoint, params):
    request_params = dict(params)
    request_params["key"] = API_KEY
    response = requests.get(
        f"{BASE_URL}/{endpoint}", params=request_params, timeout=30
    )
    if not response.ok:
        # Do not print request URLs because they contain the API key.
        try:
            detail = response.json().get("error", {}).get("message", "")
        except ValueError:
            detail = ""
        raise RuntimeError(
            f"YouTube API {endpoint} failed with HTTP {response.status_code}: "
            f"{detail or 'no error detail returned'}"
        )
    return response.json()


def discover_incremental_channels(baseline_ids):
    found = {}
    query_result_ids = Counter()
    for term in SEARCH_TERMS:
        print(f"Searching: {term}")
        data = youtube_get(
            "search",
            {
                "part": "snippet",
                "type": "channel",
                "q": term,
                "maxResults": RESULTS_PER_SEARCH,
            },
        )
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            channel_id = snippet.get("channelId")
            if not channel_id:
                continue
            query_result_ids[term] += 1
            if channel_id in baseline_ids:
                continue
            if channel_id not in found:
                found[channel_id] = {
                    "channel_id": channel_id,
                    "name": snippet.get("title", ""),
                    "description": snippet.get("description", ""),
                    "profile_url": f"https://www.youtube.com/channel/{channel_id}",
                    "discovery_terms": [],
                }
            if term not in found[channel_id]["discovery_terms"]:
                found[channel_id]["discovery_terms"].append(term)
        time.sleep(0.1)
    return found, query_result_ids


def enrich_channel_statistics(channels):
    items = list(channels.values())
    for start in range(0, len(items), 50):
        batch = items[start:start + 50]
        data = youtube_get(
            "channels",
            {
                "part": "snippet,statistics,contentDetails",
                "id": ",".join(channel["channel_id"] for channel in batch),
                "maxResults": 50,
            },
        )
        for item in data.get("items", []):
            channel = channels[item["id"]]
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            channel["name"] = snippet.get("title", channel["name"])
            channel["description"] = snippet.get("description", channel["description"])
            channel["subscribers"] = stats.get("subscriberCount", "")
            channel["subscribers_hidden"] = stats.get("hiddenSubscriberCount", False)
            channel["view_count"] = stats.get("viewCount", "")
            channel["video_count"] = stats.get("videoCount", "")
            channel["uploads_playlist_id"] = (
                item.get("contentDetails", {})
                .get("relatedPlaylists", {})
                .get("uploads", "")
            )
    return channels


def get_recent_videos(channel):
    playlist_id = channel.get("uploads_playlist_id")
    if not playlist_id:
        return []
    playlist_data = youtube_get(
        "playlistItems",
        {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": 10,
        },
    )
    video_ids = [
        item.get("contentDetails", {}).get("videoId")
        for item in playlist_data.get("items", [])
    ]
    video_ids = [video_id for video_id in video_ids if video_id]
    if not video_ids:
        return []
    video_data = youtube_get(
        "videos",
        {
            "part": "snippet,statistics",
            "id": ",".join(video_ids),
            "maxResults": 50,
        },
    )
    videos = []
    for item in video_data.get("items", []):
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        views = int(stats.get("viewCount", 0))
        likes_text = stats.get("likeCount", "")
        comments_text = stats.get("commentCount", "")
        likes = int(likes_text) if str(likes_text).isdigit() else None
        comments = int(comments_text) if str(comments_text).isdigit() else None
        rate = (
            round((likes + comments) / views * 100, 2)
            if views > 0 and likes is not None and comments is not None
            else ""
        )
        videos.append({
            "video_id": item["id"],
            "title": snippet.get("title", ""),
            "description": snippet.get("description", ""),
            "published_at": snippet.get("publishedAt", ""),
            "views": views,
            "likes": likes if likes is not None else "",
            "comments": comments if comments is not None else "",
            "engagement_rate_percent": rate,
        })
    return videos


def instagram_profile_candidates(text):
    urls = []
    for candidate in INSTAGRAM_RE.findall(text or ""):
        candidate = candidate.rstrip(").;!?]")
        parsed = urlparse(candidate)
        parts = [part for part in parsed.path.split("/") if part]
        if parts and parts[0].casefold() not in {
            "p", "reel", "reels", "stories", "explore", "accounts"
        }:
            urls.append(f"https://www.instagram.com/{parts[0]}/")
    return sorted(set(urls), key=str.casefold)


def save_csv(path, rows, fields):
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main():
    if not API_KEY:
        raise RuntimeError("YOUTUBE_API_KEY is missing. Check your local .env file.")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    baseline_channels = read_csv(BASELINE_CHANNELS)
    baseline_videos = read_csv(BASELINE_VIDEOS)
    baseline_ids = {
        row.get("channel_id", "").strip()
        for row in baseline_channels
        if row.get("channel_id", "").strip()
    }
    baseline_eligible = {
        row["channel_id"] for row in baseline_channels
        if row.get("subscribers", "").isdigit()
        and MIN_SUBSCRIBERS <= int(row["subscribers"]) <= MAX_SUBSCRIBERS
    }
    print(f"Baseline channels: {len(baseline_ids)}")
    print(f"Baseline size-eligible channels: {len(baseline_eligible)}")

    new_channels, per_query_results = discover_incremental_channels(baseline_ids)
    print(f"New unique channels (excluding baseline): {len(new_channels)}")
    if new_channels:
        new_channels = enrich_channel_statistics(new_channels)

    video_rows = []
    channel_rows = []
    eligible_count = 0
    email_count = 0
    instagram_count = 0
    error_count = 0

    channel_fields = [
        "channel_id", "name", "profile_url", "description", "subscribers",
        "subscribers_hidden", "view_count", "video_count", "discovery_terms",
        "size_filter", "size_filter_reason", "recent_video_count",
        "average_recent_video_engagement_percent", "public_email_candidates",
        "instagram_profile_candidates", "manual_relevance_review",
        "final_qualification",
    ]

    eligible_channels = []
    for channel in new_channels.values():
        raw_subs = str(channel.get("subscribers", ""))
        subscribers = int(raw_subs) if raw_subs.isdigit() else None
        if subscribers is None:
            size_filter, reason = "FAIL", "Subscriber count unavailable"
        elif subscribers < MIN_SUBSCRIBERS:
            size_filter, reason = "FAIL", "Below 5,000 subscribers"
        elif subscribers > MAX_SUBSCRIBERS:
            size_filter, reason = "FAIL", "Above 100,000 subscribers"
        else:
            size_filter, reason = "PASS", "Within 5,000–100,000 subscriber range"
            eligible_count += 1
            eligible_channels.append(channel)

        emails = sorted(set(EMAIL_RE.findall(channel.get("description", ""))), key=str.casefold)
        instagram_urls = instagram_profile_candidates(channel.get("description", ""))
        if size_filter == "PASS":
            email_count += bool(emails)
            instagram_count += bool(instagram_urls)
        channel.update({
            "size_filter": size_filter,
            "size_filter_reason": reason,
            "recent_video_count": 0,
            "average_recent_video_engagement_percent": "",
            "public_email_candidates": "; ".join(emails),
            "instagram_profile_candidates": "; ".join(instagram_urls),
            "manual_relevance_review": "REQUIRED",
            "final_qualification": "NOT YET QUALIFIED",
            "recent_videos": [],
        })

    for index, channel in enumerate(eligible_channels, start=1):
        print(f"[{index}/{len(eligible_channels)}] Fetching recent videos: {channel['name']}")
        try:
            videos = get_recent_videos(channel)
            channel["recent_videos"] = videos
            channel["recent_video_count"] = len(videos)
            rates = [
                video["engagement_rate_percent"] for video in videos
                if video["engagement_rate_percent"] != ""
            ]
            if rates:
                channel["average_recent_video_engagement_percent"] = round(
                    sum(rates) / len(rates), 2
                )
            for video in videos:
                video_rows.append({
                    "channel_id": channel["channel_id"],
                    "channel_name": channel["name"],
                    "channel_url": channel["profile_url"],
                    **video,
                })
        except (requests.RequestException, RuntimeError, ValueError) as error:
            error_count += 1
            print(f"  Recent-video fetch failed for this channel ({type(error).__name__}).")
        time.sleep(0.1)

    for channel in new_channels.values():
        row = dict(channel)
        row["discovery_terms"] = "; ".join(row.get("discovery_terms", []))
        row.pop("recent_videos", None)
        channel_rows.append(row)

    save_csv(OUTPUT_CHANNELS, channel_rows, channel_fields)
    save_csv(
        OUTPUT_VIDEOS, video_rows,
        ["channel_id", "channel_name", "channel_url", "video_id", "title",
         "description", "published_at", "views", "likes", "comments",
         "engagement_rate_percent"],
    )

    timestamp = datetime.now(timezone.utc).isoformat()
    summary = {
        "run_timestamp_utc": timestamp,
        "baseline_unique_channels": len(baseline_ids),
        "baseline_size_eligible_channels": len(baseline_eligible),
        "search_queries": SEARCH_TERMS,
        "results_per_query": RESULTS_PER_SEARCH,
        "search_result_items_by_query": dict(per_query_results),
        "new_unique_channels_excluding_baseline": len(new_channels),
        "new_channels_with_subscriber_count": sum(
            str(c.get("subscribers", "")).isdigit() for c in new_channels.values()
        ),
        "new_channels_with_5000_to_100000_subscribers": eligible_count,
        "size_eligible_with_email_pattern_in_channel_description": email_count,
        "size_eligible_with_instagram_profile_candidate_in_channel_description": instagram_count,
        "size_eligible_with_recent_video_records": sum(
            int(c.get("recent_video_count", 0)) > 0 for c in eligible_channels
        ),
        "recent_video_records_collected": len(video_rows),
        "recent_video_fetch_errors": error_count,
        "important_note": (
            "Email patterns and Instagram links are unverified candidates. "
            "Keyword/search matching is not final niche qualification. "
            "Review profile and recent video evidence manually; do not guess missing data."
        ),
    }
    with OUTPUT_JSON.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    summary_rows = [
        {"metric": key, "value": json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value}
        for key, value in summary.items()
    ]
    save_csv(OUTPUT_SUMMARY, summary_rows, ["metric", "value"])

    print("\n=== Expanded Discovery Summary ===")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nSaved: {OUTPUT_CHANNELS}")
    print(f"Saved: {OUTPUT_VIDEOS}")
    print(f"Saved: {OUTPUT_SUMMARY}")
    print(f"Saved: {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
