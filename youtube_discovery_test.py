import csv
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("YOUTUBE_API_KEY")

if not API_KEY:
    raise RuntimeError(
        "YOUTUBE_API_KEY is missing. Check your .env file."
    )

BASE_URL = "https://www.googleapis.com/youtube/v3"

SEARCH_TERMS = [
    "AI tools",
    "Python programming",
    "machine learning",
    "software engineering",
    "tech reviews",
    "developer tools",
]

MIN_SUBSCRIBERS = 5_000
MAX_SUBSCRIBERS = 100_000
RESULTS_PER_SEARCH = 25

OUTPUT_DIR = Path("data/feasibility")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def youtube_get(endpoint, params):
    """Make a request to the YouTube Data API."""

    params["key"] = API_KEY

    response = requests.get(
        f"{BASE_URL}/{endpoint}",
        params=params,
        timeout=30,
    )

    if not response.ok:
        print(f"\nAPI request failed: {response.status_code}")
        print(response.text[:1000])
        response.raise_for_status()

    return response.json()


def discover_channels():
    """Search multiple keywords and deduplicate channels."""

    channels = {}

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
            channel_id = item["snippet"]["channelId"]

            if channel_id not in channels:
                channels[channel_id] = {
                    "channel_id": channel_id,
                    "name": item["snippet"]["title"],
                    "description": item["snippet"].get(
                        "description", ""
                    ),
                    "discovery_terms": [term],
                    "profile_url": (
                        f"https://www.youtube.com/channel/{channel_id}"
                    ),
                }
            elif term not in channels[channel_id]["discovery_terms"]:
                channels[channel_id]["discovery_terms"].append(term)

        time.sleep(0.1)

    return channels


def enrich_channel_statistics(channels):
    """Fetch channel statistics in batches of up to 50 IDs."""

    channel_list = list(channels.values())

    for start in range(0, len(channel_list), 50):
        batch = channel_list[start : start + 50]

        data = youtube_get(
            "channels",
            {
                "part": "snippet,statistics,contentDetails",
                "id": ",".join(
                    channel["channel_id"] for channel in batch
                ),
                "maxResults": 50,
            },
        )

        for item in data.get("items", []):
            channel_id = item["id"]
            channel = channels[channel_id]

            statistics = item.get("statistics", {})
            snippet = item.get("snippet", {})

            channel["name"] = snippet.get(
                "title", channel["name"]
            )
            channel["description"] = snippet.get(
                "description", channel["description"]
            )

            # Subscriber counts may be hidden by the channel owner.
            channel["subscribers_hidden"] = statistics.get(
                "hiddenSubscriberCount", False
            )

            # Missing subscriberCount remains unavailable.
            channel["subscribers"] = statistics.get(
                "subscriberCount", ""
            )

            channel["view_count"] = statistics.get(
                "viewCount", ""
            )

            channel["video_count"] = statistics.get(
                "videoCount", ""
            )

            channel["uploads_playlist_id"] = (
                item.get("contentDetails", {})
                .get("relatedPlaylists", {})
                .get("uploads", "")
            )

    return channels


def get_recent_videos(channel):
    """Fetch recent public uploads and their observable statistics."""

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
        likes = int(stats.get("likeCount", 0))
        comments = int(stats.get("commentCount", 0))

        engagement_rate = (
            round((likes + comments) / views * 100, 2)
            if views > 0
            else None
        )

        videos.append(
            {
                "video_id": item["id"],
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "published_at": snippet.get("publishedAt", ""),
                "views": views,
                "likes": likes,
                "comments": comments,
                "engagement_rate_percent": engagement_rate,
            }
        )

    return videos


def save_csv(path, records, fieldnames):
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )

        writer.writeheader()
        writer.writerows(records)


def main():
    print("\n=== YouTube Discovery Feasibility Test ===\n")

    channels = discover_channels()

    print(f"\nUnique channels discovered: {len(channels)}")

    channels = enrich_channel_statistics(channels)

    all_records = []
    eligible_channels = []

    for channel in channels.values():
        subscribers = channel.get("subscribers", "")

        if subscribers != "":
            subscribers = int(subscribers)
        else:
            subscribers = None

        channel["qualification_status"] = "FAIL"
        channel["qualification_reason"] = ""

        if subscribers is None:
            channel["qualification_reason"] = (
                "Subscriber count unavailable"
            )
        elif subscribers < MIN_SUBSCRIBERS:
            channel["qualification_reason"] = (
                "Below minimum subscriber threshold"
            )
        elif subscribers > MAX_SUBSCRIBERS:
            channel["qualification_reason"] = (
                "Above maximum subscriber threshold"
            )
        else:
            channel["qualification_status"] = "PASS"
            channel["qualification_reason"] = (
                "Within micro-influencer subscriber range"
            )
            eligible_channels.append(channel)

        channel["recent_videos"] = []
        channel["recent_video_count"] = 0
        channel["average_recent_video_engagement_percent"] = ""

        all_records.append(channel)

    print(
        f"Within subscriber range: {len(eligible_channels)}"
    )

    video_records = []

    for index, channel in enumerate(eligible_channels, start=1):
        print(
            f"[{index}/{len(eligible_channels)}] "
            f"Fetching videos: {channel['name']}"
        )

        try:
            videos = get_recent_videos(channel)

            channel["recent_videos"] = videos
            channel["recent_video_count"] = len(videos)

            engagement_rates = [
                video["engagement_rate_percent"]
                for video in videos
                if video["engagement_rate_percent"] is not None
            ]

            if engagement_rates:
                channel[
                    "average_recent_video_engagement_percent"
                ] = round(
                    sum(engagement_rates) / len(engagement_rates),
                    2,
                )

            for video in videos:
                video_records.append(
                    {
                        "channel_id": channel["channel_id"],
                        "channel_name": channel["name"],
                        "channel_url": channel["profile_url"],
                        **video,
                    }
                )

        except requests.RequestException as error:
            channel["video_fetch_error"] = str(error)
            print(f"  Video retrieval failed: {error}")

    channel_fields = [
        "channel_id",
        "name",
        "profile_url",
        "description",
        "subscribers",
        "subscribers_hidden",
        "view_count",
        "video_count",
        "discovery_terms",
        "qualification_status",
        "qualification_reason",
        "recent_video_count",
        "average_recent_video_engagement_percent",
        "uploads_playlist_id",
    ]

    # Keep the CSV clean: omit nested video lists.
    channel_rows = []

    for channel in all_records:
        row = dict(channel)
        row["discovery_terms"] = "; ".join(
            row.get("discovery_terms", [])
        )
        channel_rows.append(row)

    save_csv(
        OUTPUT_DIR / "youtube_candidates.csv",
        channel_rows,
        channel_fields,
    )

    save_csv(
        OUTPUT_DIR / "youtube_recent_videos.csv",
        video_records,
        [
            "channel_id",
            "channel_name",
            "channel_url",
            "video_id",
            "title",
            "description",
            "published_at",
            "views",
            "likes",
            "comments",
            "engagement_rate_percent",
        ],
    )

    summary = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "search_terms": "; ".join(SEARCH_TERMS),
        "unique_channels_discovered": len(channels),
        "subscriber_count_available": sum(
            bool(channel.get("subscribers"))
            for channel in all_records
        ),
        "within_subscriber_range": len(eligible_channels),
        "recent_videos_collected": len(video_records),
        "channels_with_public_email_in_description": sum(
            "@" in channel.get("description", "")
            for channel in all_records
        ),
    }

    save_csv(
        OUTPUT_DIR / "feasibility_summary.csv",
        [summary],
        list(summary.keys()),
    )

    print("\n=== Results ===")

    for key, value in summary.items():
        print(f"{key}: {value}")

    print(f"\nResults saved to: {OUTPUT_DIR.resolve()}")


if __name__ == "__main__":
    main()