"""Audit the first YouTube discovery experiment without inventing missing data.

Run from the repository root:
    python scripts/audit_feasibility.py

This creates data/feasibility/candidate_audit.csv and prints a concise summary.
Keyword matches are discovery hints only; they are not final qualification.
"""
import csv
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "feasibility"
CHANNELS_FILE = DATA_DIR / "youtube_candidates.csv"
VIDEOS_FILE = DATA_DIR / "youtube_recent_videos.csv"
OUTPUT_FILE = DATA_DIR / "candidate_audit.csv"

EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])", re.I)
INSTAGRAM_RE = re.compile(r"https?://(?:www\.)?instagram\.com/[^\s<>\"'|,]+", re.I)

# These terms help a human reviewer find relevant candidates. They do not
# establish audience fit, authenticity, or final qualification by themselves.
NICHE_TERMS = {
    "AI tools": ("ai tools", "artificial intelligence", "generative ai", "chatgpt", "claude", "gemini", "ai agent"),
    "Programming": ("python", "programming", "coding", "developer", "software engineer", "javascript", "web development"),
    "Machine learning": ("machine learning", "deep learning", "neural network", "data science", "llm", "rag"),
    "Tech reviews": ("tech review", "gadget", "smartphone", "laptop", "monitor", "gaming gear", "product review"),
}


def read_csv(path):
    if not path.exists():
        raise FileNotFoundError(f"Required input file not found: {path}")
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def unique_matches(pattern, text):
    return sorted(set(pattern.findall(text or "")), key=str.casefold)


def instagram_profile_urls(text):
    urls = []
    for candidate in INSTAGRAM_RE.findall(text or ""):
        candidate = candidate.rstrip(").;!?]")
        parsed = urlparse(candidate)
        # Exclude Instagram posts/reels as profile URLs; keep public profile paths.
        parts = [part for part in parsed.path.split("/") if part]
        if parts and parts[0].casefold() not in {"p", "reel", "reels", "stories", "explore"}:
            urls.append(f"https://www.instagram.com/{parts[0]}/")
    return sorted(set(urls), key=str.casefold)


def main():
    channels = read_csv(CHANNELS_FILE)
    videos = read_csv(VIDEOS_FILE)
    videos_by_channel = {}
    for video in videos:
        videos_by_channel.setdefault(video.get("channel_id", ""), []).append(video)

    audit_rows = []
    size_eligible = [
        channel for channel in channels
        if channel.get("subscribers", "").isdigit()
        and 5_000 <= int(channel["subscribers"]) <= 100_000
    ]

    for channel in channels:
        channel_videos = videos_by_channel.get(channel.get("channel_id", ""), [])
        description = channel.get("description", "")
        video_evidence = " | ".join(
            f"{video.get('title', '')} — {video.get('description', '')}"
            for video in channel_videos
        )
        all_text = f"{description}\n{video_evidence}"
        emails = unique_matches(EMAIL_RE, description)
        instagram_urls = instagram_profile_urls(all_text)
        niche_matches = [
            niche for niche, terms in NICHE_TERMS.items()
            if any(term in all_text.casefold() for term in terms)
        ]
        subscribers_text = channel.get("subscribers", "")
        subscribers = int(subscribers_text) if subscribers_text.isdigit() else None
        size_pass = subscribers is not None and 5_000 <= subscribers <= 100_000

        audit_rows.append({
            "channel_id": channel.get("channel_id", ""),
            "channel_name": channel.get("name", ""),
            "youtube_url": channel.get("profile_url", ""),
            "subscribers": subscribers_text,
            "size_filter": "PASS" if size_pass else "FAIL",
            "discovery_terms": channel.get("discovery_terms", ""),
            "recent_videos_available": len(channel_videos),
            "sample_video_titles": " | ".join(
                video.get("title", "") for video in channel_videos[:5]
            ),
            "keyword_niche_hints": "; ".join(niche_matches),
            "public_email_candidates": "; ".join(emails),
            "email_source": "YouTube channel description" if emails else "Not Found",
            "instagram_profile_candidates": "; ".join(instagram_urls),
            "manual_relevance_review": "REQUIRED",
            "final_qualification": "NOT YET QUALIFIED",
        })

    fieldnames = list(audit_rows[0].keys()) if audit_rows else []
    with OUTPUT_FILE.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(audit_rows)

    eligible_rows = [row for row in audit_rows if row["size_filter"] == "PASS"]
    summary = {
        "channels_discovered": len(channels),
        "channels_with_5k_to_100k_subscribers": len(eligible_rows),
        "size_eligible_with_email_pattern_in_channel_description": sum(
            bool(row["public_email_candidates"]) for row in eligible_rows
        ),
        "size_eligible_with_instagram_profile_candidate_in_available_text": sum(
            bool(row["instagram_profile_candidates"]) for row in eligible_rows
        ),
        "size_eligible_with_recent_video_records": sum(
            int(row["recent_videos_available"]) > 0 for row in eligible_rows
        ),
        "keyword_niche_hint_counts": dict(Counter(
            niche
            for row in eligible_rows
            for niche in row["keyword_niche_hints"].split("; ")
            if niche
        )),
        "important_note": (
            "These are feasibility signals, not final qualification. "
            "Email-pattern detection does not verify deliverability; "
            "Instagram links and niche relevance require manual review."
        ),
        "output_file": str(OUTPUT_FILE.relative_to(ROOT)),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nAudit CSV saved to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
