"""Build a conservative contact-enrichment queue from publicly observed YouTube data.

Run from the repository root:
    python scripts/enrich_candidates.py

Input:
    data/feasibility/campaign_qualification_queue.csv

Outputs:
    data/feasibility/enriched_influencers.csv
    data/feasibility/enrichment_summary.json

This is an enrichment queue, not proof that a creator is qualified or reachable.
It does not crawl external websites, guess emails, validate deliverability, or
automatically confirm Instagram identities. Every candidate contact needs human
verification against a public source before outreach.
"""
import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
INPUT_FILE = ROOT / "data" / "feasibility" / "campaign_qualification_queue.csv"
OUTPUT_FILE = ROOT / "data" / "feasibility" / "enriched_influencers.csv"
SUMMARY_FILE = ROOT / "data" / "feasibility" / "enrichment_summary.json"

URL_RE = re.compile(r"""https?://[^\s<>"'|]+""", re.I)
EXCLUDED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    "instagram.com", "www.instagram.com", "facebook.com", "www.facebook.com",
    "tiktok.com", "www.tiktok.com", "twitter.com", "www.twitter.com",
    "x.com", "www.x.com", "linkedin.com", "www.linkedin.com",
    "youtu.be", "bit.ly", "linktr.ee",
}


def read_rows(path):
    if not path.exists():
        raise FileNotFoundError(
            f"Missing input file: {path}\n"
            "Run scripts/build_candidate_review_queue.py and then "
            "scripts/qualify_candidates.py first."
        )
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def clean_url(candidate):
    return candidate.rstrip(").,;!?]}>").strip()


def website_candidates(description):
    """Return external website URLs seen literally in the channel description."""
    found = []
    for raw_url in URL_RE.findall(description or ""):
        url = clean_url(raw_url)
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").casefold()
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"} or not host:
            continue
        if host in EXCLUDED_HOSTS or any(host.endswith("." + item) for item in EXCLUDED_HOSTS):
            continue
        # Avoid treating obvious URL shorteners as a creator's own website.
        if host in {"t.co", "tinyurl.com", "shorturl.at", "ow.ly"}:
            continue
        if url not in found:
            found.append(url)
    return found


def main():
    rows = read_rows(INPUT_FILE)
    enriched = []
    counts = Counter()

    for row in rows:
        # This output is a practical working queue, limited to the assignment's
        # stated micro-influencer range. Failed-size records remain in the source.
        if row.get("subscriber_range_filter") != "PASS":
            continue

        email_value = (row.get("public_email_candidate") or "").strip()
        instagram_value = (row.get("instagram_profile_candidate") or "").strip()
        description = row.get("channel_description_evidence", "")
        websites = website_candidates(description)

        email = email_value if email_value else "Not Found"
        email_status = (
            "PUBLIC EMAIL PATTERN FOUND — VERIFY SOURCE AND CREATOR IDENTITY"
            if email_value else "Not Found"
        )
        instagram_status = (
            "PROFILE URL CANDIDATE — VERIFY IT BELONGS TO THIS CREATOR"
            if instagram_value else "Not Found"
        )
        website_status = (
            "URL FOUND IN CHANNEL DESCRIPTION — OWNERSHIP NOT VERIFIED"
            if websites else "Not Found"
        )

        result = {
            "influencer_name": row.get("influencer_name", ""),
            "platform": row.get("platform", "YouTube"),
            "profile_url": row.get("profile_url", ""),
            "follower_count": row.get("follower_count", ""),
            "follower_count_source": row.get("follower_count_source", "YouTube Data API"),
            "subscriber_range_filter": row.get("subscriber_range_filter", ""),
            "campaign_relevance_screen": row.get("campaign_relevance_screen", ""),
            "relevance_terms_matched": row.get("campaign_relevance_terms_matched", ""),
            "recent_video_count": row.get("recent_video_count", ""),
            "recent_video_title_evidence": row.get("recent_video_title_evidence", ""),
            "view_interaction_percent_proxy": row.get("average_recent_video_view_interaction_percent", ""),
            "engagement_metric_note": row.get("interaction_metric_note", ""),
            "niche_keyword_hints_not_verified": row.get("niche_keyword_hints_not_verified", ""),
            "content_themes": row.get("recent_video_title_evidence", "") or "Not Found",
            "contact_email": email,
            "contact_email_status": email_status,
            "contact_email_source": row.get("email_source", "Not Found") if email_value else "Not Found",
            "contact_email_source_url": row.get("profile_url", "") if email_value else "Not Found",
            "instagram_profile_candidate": instagram_value or "Not Found",
            "instagram_profile_status": instagram_status,
            "external_website_candidates": "; ".join(websites) if websites else "Not Found",
            "website_candidate_status": website_status,
            "public_contact_verification": "PENDING MANUAL VERIFICATION",
            "creator_identity_verification": "PENDING MANUAL VERIFICATION",
            "human_content_review": "PENDING",
            "final_qualification": "NOT YET QUALIFIED",
            "outreach_status": "NOT STARTED",
            "reviewer_notes": "",
        }
        enriched.append(result)
        counts["size_eligible_rows"] += 1
        counts["email_candidate_rows"] += bool(email_value)
        counts["instagram_candidate_rows"] += bool(instagram_value)
        counts["website_candidate_rows"] += bool(websites)
        counts["high_relevance_rows"] += row.get("campaign_relevance_screen") == "HIGH RELEVANCE SIGNAL — REVIEW REQUIRED"
        counts["possible_relevance_rows"] += row.get("campaign_relevance_screen") == "POSSIBLE RELEVANCE — REVIEW REQUIRED"

    # Review high relevance signals first, then possible relevance, then other eligible rows.
    priority = {
        "HIGH RELEVANCE SIGNAL — REVIEW REQUIRED": 0,
        "POSSIBLE RELEVANCE — REVIEW REQUIRED": 1,
        "MANUAL REVIEW — POSSIBLE CONTENT MISMATCH": 2,
        "LOW KEYWORD SIGNAL — REVIEW BEFORE EXCLUDING": 3,
    }
    enriched.sort(key=lambda item: (
        priority.get(item["campaign_relevance_screen"], 4),
        item["influencer_name"].casefold(),
    ))

    fields = list(enriched[0].keys()) if enriched else []
    with OUTPUT_FILE.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(enriched)

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_rows": len(rows),
        "size_eligible_rows_in_enrichment_queue": counts["size_eligible_rows"],
        "high_relevance_signal_rows_for_review": counts["high_relevance_rows"],
        "possible_relevance_rows_for_review": counts["possible_relevance_rows"],
        "rows_with_public_email_pattern_candidates_unverified": counts["email_candidate_rows"],
        "rows_with_instagram_profile_candidates_unverified": counts["instagram_candidate_rows"],
        "rows_with_external_website_urls_in_channel_description": counts["website_candidate_rows"],
        "final_qualified_influencers": 0,
        "policy": (
            "Only size-eligible rows are included. Contact candidates are copied from observed "
            "public text and remain unverified. External URLs are extracted from channel "
            "descriptions only; websites are not fetched. Missing contacts are recorded as "
            "'Not Found'. Human review is required before qualification or outreach."
        ),
        "output_file": str(OUTPUT_FILE.relative_to(ROOT)),
    }
    with SUMMARY_FILE.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nEnrichment queue saved to: {OUTPUT_FILE}")
    print(f"Summary saved to: {SUMMARY_FILE}")


if __name__ == "__main__":
    main()
