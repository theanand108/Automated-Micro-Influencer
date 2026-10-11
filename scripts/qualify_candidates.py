"""Apply configurable, transparent relevance signals to the candidate review queue.

Run from the repository root:
    python scripts/qualify_candidates.py

Inputs:
    config/brand_config.json
    data/feasibility/candidate_review_queue.csv

Outputs:
    data/feasibility/campaign_qualification_queue.csv
    data/feasibility/campaign_qualification_summary.json

This script does NOT automatically qualify influencers. It prioritizes records for
human review using explicit keyword evidence and subscriber-range checks. Keywords
are not proof of audience fit, and contact details remain unverified until checked.
"""
import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "config" / "brand_config.json"
INPUT_FILE = ROOT / "data" / "feasibility" / "candidate_review_queue.csv"
OUTPUT_FILE = ROOT / "data" / "feasibility" / "campaign_qualification_queue.csv"
SUMMARY_FILE = ROOT / "data" / "feasibility" / "campaign_qualification_summary.json"


def read_csv(path):
    if not path.exists():
        raise FileNotFoundError(
            f"Missing input file: {path}\n"
            "Run scripts/build_candidate_review_queue.py first."
        )
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def normalized(value):
    return re.sub(r"\s+", " ", str(value or "")).casefold()


def main():
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"Campaign configuration not found: {CONFIG_FILE}")
    with CONFIG_FILE.open("r", encoding="utf-8") as handle:
        config = json.load(handle)

    rows = read_csv(INPUT_FILE)
    relevance_terms = [
        term.strip() for term in config.get("relevance_terms", []) if term.strip()
    ]
    exclusion_terms = [
        term.strip() for term in config.get("exclusion_terms", []) if term.strip()
    ]
    min_followers = int(config.get("minimum_followers", 5000))
    max_followers = int(config.get("maximum_followers", 100000))
    policy = config.get("qualification_policy", {})
    high_min = int(policy.get("high_relevance_minimum_distinct_terms", 3))
    possible_min = int(policy.get("possible_relevance_minimum_distinct_terms", 1))

    output_rows = []
    relevance_counts = Counter()
    size_eligible_count = 0
    high_count = 0
    possible_count = 0
    low_count = 0
    email_candidates = 0
    instagram_candidates = 0

    for row in rows:
        description = row.get("channel_description_evidence", "")
        titles = row.get("recent_video_title_evidence", "")
        evidence = normalized(f"{description} {titles}")
        matched_terms = [term for term in relevance_terms if normalized(term) in evidence]
        matched_exclusions = [term for term in exclusion_terms if normalized(term) in evidence]

        raw_followers = str(row.get("follower_count", "")).strip()
        followers = int(raw_followers) if raw_followers.isdigit() else None
        if followers is None:
            size_status = "FAIL"
            size_reason = "Follower/subscriber count unavailable"
        elif followers < min_followers:
            size_status = "FAIL"
            size_reason = f"Below minimum of {min_followers}"
        elif followers > max_followers:
            size_status = "FAIL"
            size_reason = f"Above maximum of {max_followers}"
        else:
            size_status = "PASS"
            size_reason = f"Within {min_followers}–{max_followers} range"
            size_eligible_count += 1

        if size_status != "PASS":
            relevance_status = "NOT ASSESSED — SIZE FILTER FAILED"
            next_action = "EXCLUDE"
        elif matched_exclusions:
            relevance_status = "MANUAL REVIEW — POSSIBLE CONTENT MISMATCH"
            next_action = "REVIEW EXCLUSION EVIDENCE"
        elif len(matched_terms) >= high_min:
            relevance_status = "HIGH RELEVANCE SIGNAL — REVIEW REQUIRED"
            next_action = "PRIORITIZE MANUAL REVIEW"
            high_count += 1
        elif len(matched_terms) >= possible_min:
            relevance_status = "POSSIBLE RELEVANCE — REVIEW REQUIRED"
            next_action = "MANUAL REVIEW"
            possible_count += 1
        else:
            relevance_status = "LOW KEYWORD SIGNAL — REVIEW BEFORE EXCLUDING"
            next_action = "CHECK CONTENT OR EXCLUDE WITH REASON"
            low_count += 1

        email = (row.get("public_email_candidate") or "").strip()
        instagram = (row.get("instagram_profile_candidate") or "").strip()
        email_candidates += bool(email)
        instagram_candidates += bool(instagram)
        relevance_counts[relevance_status] += 1

        new_row = dict(row)
        new_row.update({
            "campaign_name": config.get("campaign_name", ""),
            "brand_name": config.get("brand_name", ""),
            "target_niches": "; ".join(config.get("target_niches", [])),
            "campaign_relevance_terms_matched": "; ".join(matched_terms),
            "potential_exclusion_terms_matched": "; ".join(matched_exclusions),
            "distinct_relevance_term_count": len(matched_terms),
            "campaign_relevance_screen": relevance_status,
            "subscriber_filter_reason": size_reason,
            "next_review_action": next_action,
            "human_review_decision": "PENDING" if size_status == "PASS" else "FAIL",
            "human_review_reason": "",
            "contact_verification_status": (
                "PENDING — VERIFY PUBLIC SOURCE" if (email or instagram)
                else "NOT FOUND IN CURRENT DATASET"
            ),
            "final_qualification": (
                "NOT YET QUALIFIED" if size_status == "PASS" else "FAIL"
            ),
        })
        output_rows.append(new_row)

    if output_rows:
        fields = list(output_rows[0].keys())
        with OUTPUT_FILE.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(sorted(
                output_rows,
                key=lambda row: (
                    row.get("subscriber_range_filter") != "PASS",
                    {
                        "HIGH RELEVANCE SIGNAL — REVIEW REQUIRED": 0,
                        "POSSIBLE RELEVANCE — REVIEW REQUIRED": 1,
                        "MANUAL REVIEW — POSSIBLE CONTENT MISMATCH": 2,
                        "LOW KEYWORD SIGNAL — REVIEW BEFORE EXCLUDING": 3,
                    }.get(row["campaign_relevance_screen"], 4),
                    row.get("influencer_name", "").casefold(),
                ),
            ))

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "campaign_name": config.get("campaign_name", ""),
        "brand_name": config.get("brand_name", ""),
        "target_niches": config.get("target_niches", []),
        "input_rows": len(rows),
        "subscriber_range": {"minimum": min_followers, "maximum": max_followers},
        "size_eligible_rows": size_eligible_count,
        "high_relevance_signal_rows_requiring_review": high_count,
        "possible_relevance_rows_requiring_review": possible_count,
        "low_keyword_signal_rows_requiring_review": low_count,
        "email_candidate_rows_unverified": email_candidates,
        "instagram_candidate_rows_unverified": instagram_candidates,
        "relevance_status_counts": dict(relevance_counts),
        "final_qualified_influencers": 0,
        "qualification_policy": (
            "No automatic final qualification. Human review must verify content relevance, "
            "brand fit, creator identity, and contact source. Keyword matches only prioritize review."
        ),
        "output_file": str(OUTPUT_FILE.relative_to(ROOT)),
    }
    with SUMMARY_FILE.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nQualification queue saved to: {OUTPUT_FILE}")
    print(f"Summary saved to: {SUMMARY_FILE}")


if __name__ == "__main__":
    main()
