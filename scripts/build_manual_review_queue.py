"""Prepare a human-review worksheet for size-eligible campaign candidates.

Run from repository root after rebuilding qualification and enrichment outputs:
    python scripts/build_manual_review_queue.py

Inputs:
    data/feasibility/campaign_qualification_queue.csv
    data/feasibility/enriched_influencers.csv

Output:
    data/feasibility/manual_review_queue.csv

This script never auto-qualifies creators. Reviewers must verify content fit, identity,
and contact evidence, then record PASS/FAIL and a reason. The fictional campaign must
be disclosed in any demo materials.
"""
import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUALIFICATION_FILE = ROOT / "data" / "feasibility" / "campaign_qualification_queue.csv"
ENRICHED_FILE = ROOT / "data" / "feasibility" / "enriched_influencers.csv"
OUTPUT_FILE = ROOT / "data" / "feasibility" / "manual_review_queue.csv"

PRIORITY = {
    "HIGH RELEVANCE SIGNAL — REVIEW REQUIRED": 0,
    "POSSIBLE RELEVANCE — REVIEW REQUIRED": 1,
    "MANUAL REVIEW — POSSIBLE CONTENT MISMATCH": 2,
    "LOW KEYWORD SIGNAL — REVIEW BEFORE EXCLUDING": 3,
}


def read_csv(path):
    if not path.exists():
        raise FileNotFoundError(
            f"Missing input file: {path}\n"
            "Run scripts/qualify_candidates.py and scripts/enrich_candidates.py first."
        )
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def main():
    qualified_rows = read_csv(QUALIFICATION_FILE)
    enriched_rows = read_csv(ENRICHED_FILE)
    enriched_by_profile = {
        row.get("profile_url", "").strip(): row
        for row in enriched_rows
        if row.get("profile_url", "").strip()
    }

    output = []
    for row in qualified_rows:
        if row.get("subscriber_range_filter") != "PASS":
            continue
        extra = enriched_by_profile.get(row.get("profile_url", "").strip(), {})
        output.append({
            "influencer_name": row.get("influencer_name", ""),
            "platform": row.get("platform", "YouTube"),
            "profile_url": row.get("profile_url", ""),
            "subscriber_count": row.get("follower_count", ""),
            "subscriber_count_source": row.get("follower_count_source", "YouTube Data API"),
            "campaign_relevance_signal": row.get("campaign_relevance_screen", ""),
            "matched_relevance_terms": row.get("campaign_relevance_terms_matched", ""),
            "potential_exclusion_terms": row.get("potential_exclusion_terms_matched", ""),
            "channel_description_evidence": row.get("channel_description_evidence", ""),
            "recent_video_titles_evidence": row.get("recent_video_title_evidence", ""),
            "view_interaction_percent_proxy": row.get("average_recent_video_view_interaction_percent", ""),
            "engagement_metric_note": row.get("interaction_metric_note", ""),
            "email_candidate": extra.get("contact_email", "Not Found"),
            "email_candidate_status": extra.get("contact_email_status", "Not Found"),
            "email_source_url": extra.get("contact_email_source_url", "Not Found"),
            "instagram_profile_candidate": extra.get("instagram_profile_candidate", "Not Found"),
            "instagram_profile_status": extra.get("instagram_profile_status", "Not Found"),
            "external_website_candidates": extra.get("external_website_candidates", "Not Found"),
            "review_content_relevance": "PENDING",
            "review_creator_identity": "PENDING",
            "review_contact_source": "PENDING",
            "review_campaign_fit": "PENDING",
            "review_decision": "PENDING",
            "review_reason": "",
            "reviewer_notes": "",
            "final_qualification": "NOT YET QUALIFIED",
            "outreach_status": "NOT STARTED",
        })

    output.sort(key=lambda row: (
        PRIORITY.get(row["campaign_relevance_signal"], 4),
        row["influencer_name"].casefold(),
    ))
    fields = list(output[0].keys()) if output else []
    with OUTPUT_FILE.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(output)

    counts = {}
    for row in output:
        signal = row["campaign_relevance_signal"]
        counts[signal] = counts.get(signal, 0) + 1
    print(f"Manual review queue saved to: {OUTPUT_FILE}")
    print(f"Size-eligible rows ready for review: {len(output)}")
    print("Review-signal counts:")
    for label, count in counts.items():
        print(f"  {label}: {count}")
    print("No candidates were automatically qualified.")


if __name__ == "__main__":
    main()
