"""Combine baseline and expanded YouTube discovery into a transparent review queue.

Run after both discovery experiments:
    python scripts/build_candidate_review_queue.py

Required local inputs:
- data/feasibility/youtube_candidates.csv
- data/feasibility/youtube_recent_videos.csv
- data/feasibility/youtube_expanded_candidates.csv
- data/feasibility/youtube_expanded_recent_videos.csv

This script does not automatically qualify influencers. It applies only the explicit
subscriber-range gate and creates a human-review queue for content relevance/contact
verification. Never treat a found email pattern or social link as verified.
"""
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "feasibility"
INPUTS = {
    "baseline_channels": DATA_DIR / "youtube_candidates.csv",
    "baseline_videos": DATA_DIR / "youtube_recent_videos.csv",
    "expanded_channels": DATA_DIR / "youtube_expanded_candidates.csv",
    "expanded_videos": DATA_DIR / "youtube_expanded_recent_videos.csv",
}
OUTPUT_QUEUE = DATA_DIR / "candidate_review_queue.csv"
OUTPUT_SUMMARY = DATA_DIR / "candidate_review_summary.json"

MIN_SUBSCRIBERS = 5_000
MAX_SUBSCRIBERS = 100_000
EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])",
    re.I,
)
INSTAGRAM_RE = re.compile(
    r"""https?://(?:www\.)?instagram\.com/[^\s<>"'|,]+""", re.I
)

NICHE_TERMS = {
    "AI tools": (
        "ai tools", "artificial intelligence", "generative ai", "chatgpt",
        "claude", "gemini", "ai agent", "ai productivity",
    ),
    "Programming": (
        "python", "programming", "coding", "developer", "software engineer",
        "javascript", "web development", "coding projects",
    ),
    "Machine learning": (
        "machine learning", "deep learning", "neural network", "data science",
        "llm", "rag",
    ),
    "Tech reviews": (
        "tech review", "gadget", "smartphone", "laptop", "monitor",
        "gaming gear", "product review", "tech products",
    ),
}


def read_csv(path):
    if not path.exists():
        raise FileNotFoundError(
            f"Required input is missing: {path}\n"
            "Run both discovery scripts first and keep their output files in "
            "data/feasibility/. The discovery output CSVs are local generated data."
        )
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def clean_unique(values):
    return sorted({value.strip() for value in values if value and value.strip()}, key=str.casefold)


def extract_instagram_profiles(text):
    profiles = []
    for candidate in INSTAGRAM_RE.findall(text or ""):
        candidate = candidate.rstrip(").;!?]")
        parsed = urlparse(candidate)
        parts = [part for part in parsed.path.split("/") if part]
        if parts and parts[0].casefold() not in {
            "p", "reel", "reels", "stories", "explore", "accounts"
        }:
            profiles.append(f"https://www.instagram.com/{parts[0]}/")
    return clean_unique(profiles)


def parse_int(value):
    value = str(value or "").strip()
    return int(value) if value.isdigit() else None


def main():
    channel_sources = [
        ("baseline", read_csv(INPUTS["baseline_channels"])),
        ("expanded", read_csv(INPUTS["expanded_channels"])),
    ]
    video_sources = [
        read_csv(INPUTS["baseline_videos"]),
        read_csv(INPUTS["expanded_videos"]),
    ]

    merged = {}
    source_count = Counter()
    duplicate_records = 0
    for source_name, rows in channel_sources:
        for row in rows:
            channel_id = (row.get("channel_id") or "").strip()
            if not channel_id:
                continue
            source_count[source_name] += 1
            if channel_id in merged:
                duplicate_records += 1
                previous = merged[channel_id]
                terms = clean_unique(
                    (previous.get("discovery_terms", "").split(";"))
                    + (row.get("discovery_terms", "").split(";"))
                )
                previous["discovery_terms"] = "; ".join(terms)
                if not previous.get("description") and row.get("description"):
                    previous["description"] = row["description"]
                if not previous.get("subscribers") and row.get("subscribers"):
                    previous["subscribers"] = row["subscribers"]
                continue

            item = dict(row)
            item["_discovery_source"] = source_name
            item["discovery_terms"] = "; ".join(
                clean_unique((row.get("discovery_terms") or "").split(";"))
            )
            merged[channel_id] = item

    videos_by_channel = defaultdict(list)
    for rows in video_sources:
        for video in rows:
            channel_id = (video.get("channel_id") or "").strip()
            if channel_id:
                video["title"] = video.get("title", "")
                videos_by_channel[channel_id].append(video)

    # Remove duplicate videos by video_id while preserving first-seen order.
    for channel_id, videos in list(videos_by_channel.items()):
        unique_videos = []
        seen_video_ids = set()
        for video in videos:
            video_id = (video.get("video_id") or "").strip()
            key = video_id or (
                video.get("title", ""),
                video.get("published_at", ""),
            )
            if key in seen_video_ids:
                continue
            seen_video_ids.add(key)
            unique_videos.append(video)
        videos_by_channel[channel_id] = unique_videos

    queue_rows = []
    size_eligible = 0
    email_candidates_count = 0
    instagram_candidates_count = 0
    for channel_id, channel in merged.items():
        name = channel.get("name") or channel.get("channel_name") or ""
        profile_url = channel.get("profile_url") or channel.get("channel_url") or ""
        description = channel.get("description", "")
        videos = videos_by_channel.get(channel_id, [])
        video_titles = clean_unique([video.get("title", "") for video in videos])
        video_text = " ".join(
            f"{video.get('title', '')} {video.get('description', '')}"
            for video in videos
        )
        evidence_text = f"{description} {video_text}".casefold()
        niche_hints = [
            niche for niche, terms in NICHE_TERMS.items()
            if any(term in evidence_text for term in terms)
        ]

        subscribers = parse_int(channel.get("subscribers"))
        if subscribers is None:
            size_filter = "FAIL"
            size_reason = "Subscriber count unavailable"
        elif subscribers < MIN_SUBSCRIBERS:
            size_filter = "FAIL"
            size_reason = "Below 5,000 subscribers"
        elif subscribers > MAX_SUBSCRIBERS:
            size_filter = "FAIL"
            size_reason = "Above 100,000 subscribers"
        else:
            size_filter = "PASS"
            size_reason = "Within 5,000–100,000 subscriber range"
            size_eligible += 1

        emails = clean_unique(EMAIL_RE.findall(description))
        instagram_profiles = extract_instagram_profiles(
            f"{description}\n{video_text}"
        )
        if size_filter == "PASS":
            email_candidates_count += bool(emails)
            instagram_candidates_count += bool(instagram_profiles)

        # Preserve the observed rate as a view-based interaction metric; do not
        # label it a standardized audience engagement rate.
        existing_rate = channel.get("average_recent_video_engagement_percent", "")
        if existing_rate == "":
            rates = [
                float(video["engagement_rate_percent"])
                for video in videos
                if str(video.get("engagement_rate_percent", "")).strip()
                and str(video.get("engagement_rate_percent", "")).strip().replace(".", "", 1).isdigit()
            ]
            existing_rate = round(sum(rates) / len(rates), 2) if rates else ""

        queue_rows.append({
            "channel_id": channel_id,
            "influencer_name": name,
            "platform": "YouTube",
            "profile_url": profile_url,
            "follower_count": subscribers if subscribers is not None else "",
            "follower_count_source": "YouTube Data API" if subscribers is not None else "Not Found",
            "subscriber_range_filter": size_filter,
            "subscriber_filter_reason": size_reason,
            "discovery_source": channel.get("_discovery_source", ""),
            "discovery_queries": channel.get("discovery_terms", ""),
            "niche_keyword_hints_not_verified": "; ".join(niche_hints),
            "channel_description_evidence": description,
            "recent_video_count": len(videos),
            "recent_video_title_evidence": " | ".join(video_titles[:10]),
            "average_recent_video_view_interaction_percent": existing_rate,
            "interaction_metric_note": (
                "Average of available per-video (likes + comments) / views; "
                "not a standardized audience engagement rate."
            ),
            "public_email_candidate": "; ".join(emails),
            "email_source": "YouTube channel description" if emails else "Not Found",
            "email_verification_status": "UNVERIFIED" if emails else "NOT FOUND",
            "instagram_profile_candidate": "; ".join(instagram_profiles),
            "instagram_identity_verification_status": "UNVERIFIED" if instagram_profiles else "NOT FOUND",
            "content_relevance_review": "PENDING" if size_filter == "PASS" else "NOT REQUIRED — FAILED SIZE FILTER",
            "reviewer_decision": "PENDING" if size_filter == "PASS" else "FAIL",
            "reviewer_reason": "" if size_filter == "PASS" else size_reason,
            "contact_verification_status": "PENDING" if size_filter == "PASS" else "NOT REQUIRED — FAILED SIZE FILTER",
            "brand_name": "",
            "product_description": "",
            "target_niche": "",
            "personalized_outreach_status": "NOT STARTED",
            "final_qualification": "NOT YET QUALIFIED" if size_filter == "PASS" else "FAIL",
        })

    fields = list(queue_rows[0].keys()) if queue_rows else []
    with OUTPUT_QUEUE.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(sorted(
            queue_rows,
            key=lambda row: (
                row["subscriber_range_filter"] != "PASS",
                row["influencer_name"].casefold(),
            ),
        ))

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "baseline_channel_rows_read": source_count["baseline"],
        "expanded_channel_rows_read": source_count["expanded"],
        "duplicate_channel_records_removed": duplicate_records,
        "combined_unique_channels": len(merged),
        "channels_with_5000_to_100000_subscribers": size_eligible,
        "size_eligible_with_email_pattern_in_channel_description": email_candidates_count,
        "size_eligible_with_instagram_profile_candidate_in_available_text": instagram_candidates_count,
        "channels_with_recent_video_evidence": sum(
            bool(videos_by_channel.get(channel_id))
            for channel_id, row in merged.items()
            if MIN_SUBSCRIBERS <= (parse_int(row.get("subscribers")) or 0) <= MAX_SUBSCRIBERS
        ),
        "review_queue_rows": size_eligible,
        "final_qualified_influencers": 0,
        "next_step": (
            "Manually review each size-eligible channel using its description and recent-video "
            "evidence. Configure the target brand/product before making brand-fit decisions. "
            "Verify contact details from public sources. Do not treat keyword hints, email "
            "patterns, or Instagram links as verified facts."
        ),
        "output_file": str(OUTPUT_QUEUE.relative_to(ROOT)),
    }
    with OUTPUT_SUMMARY.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nReview queue saved to: {OUTPUT_QUEUE}")
    print(f"Summary saved to: {OUTPUT_SUMMARY}")


if __name__ == "__main__":
    main()
