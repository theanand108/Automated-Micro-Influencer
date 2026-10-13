"""Finalize human review decisions without auto-qualifying candidates.

Run from the repository root after manually editing
data/feasibility/manual_review_queue.csv:

    python scripts/finalize_manual_review.py

Only rows with review_decision=PASS and all four review checks=PASS are eligible.
Because this campaign requires email outreach, a syntactically valid email and
its source URL are also required. This validates recorded evidence; it does not
prove an email mailbox is deliverable. PENDING and NEEDS_MORE_EVIDENCE rows are
never included. FAIL rows need a reason for auditability.
"""
import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUT_FILE = ROOT / "data" / "feasibility" / "manual_review_queue.csv"
OUTPUT_FILE = ROOT / "data" / "feasibility" / "final_qualified_influencers.csv"
SUMMARY_FILE = ROOT / "data" / "feasibility" / "final_qualification_summary.json"

REVIEW_CHECKS = (
    "review_content_relevance",
    "review_creator_identity",
    "review_contact_source",
    "review_campaign_fit",
)
VALID_DECISIONS = {"PASS", "FAIL", "PENDING", "NEEDS_MORE_EVIDENCE"}
EMAIL_RE = re.compile(r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$", re.I)


def clean(value):
    return (value or "").strip()


def read_rows(path):
    if not path.exists():
        raise FileNotFoundError(
            f"Missing review worksheet: {path}\n"
            "Run scripts/build_manual_review_queue.py first."
        )
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"CSV has no header: {path}")
        required = {
            "influencer_name", "profile_url", "email_candidate", "email_source_url",
            "review_decision", "review_reason", *REVIEW_CHECKS,
        }
        missing = sorted(required - set(reader.fieldnames))
        if missing:
            raise ValueError("Review worksheet is missing required columns: " + ", ".join(missing))
        return list(reader), reader.fieldnames


def main():
    rows, source_fields = read_rows(INPUT_FILE)
    output = []
    decision_counts = Counter()
    check_failures = Counter()
    errors = []
    seen_profiles = set()

    for line_number, row in enumerate(rows, start=2):
        name = clean(row.get("influencer_name"))
        profile = clean(row.get("profile_url"))
        decision = clean(row.get("review_decision")).upper() or "PENDING"
        decision_counts[decision] += 1

        if decision not in VALID_DECISIONS:
            errors.append(f"Row {line_number} ({name or profile}): invalid review_decision={decision!r}")
            continue

        if decision == "FAIL" and not clean(row.get("review_reason")):
            errors.append(f"Row {line_number} ({name or profile}): FAIL requires review_reason.")
        if decision != "PASS":
            continue

        row_errors = []
        if not name:
            row_errors.append("influencer_name is empty")
        if not profile.startswith(("https://", "http://")):
            row_errors.append("profile_url must be an explicit http(s) URL")

        for field in REVIEW_CHECKS:
            value = clean(row.get(field)).upper()
            if value != "PASS":
                row_errors.append(f"{field} must be PASS (got {value or 'blank'})")
                check_failures[field] += 1

        email = clean(row.get("email_candidate"))
        email_source = clean(row.get("email_source_url"))
        if not EMAIL_RE.fullmatch(email) or email.casefold() == "not found":
            row_errors.append("a syntactically valid, explicitly sourced email is required")
        if not email_source.startswith(("https://", "http://")):
            row_errors.append("email_source_url must be an explicit http(s) source URL")

        profile_key = profile.rstrip("/").casefold()
        if profile_key and profile_key in seen_profiles:
            row_errors.append("duplicate profile_url")
        if row_errors:
            errors.append(f"Row {line_number} ({name or profile}): " + "; ".join(row_errors))
            continue

        seen_profiles.add(profile_key)
        qualified = dict(row)
        qualified["final_qualification"] = "QUALIFIED"
        qualified["outreach_status"] = clean(row.get("outreach_status")) or "NOT STARTED"
        output.append(qualified)

    if errors:
        print("Validation errors found. No output files were changed.")
        for error in errors:
            print(f"- {error}")
        raise SystemExit(1)

    # Preserve all human-review evidence and notes in the final file.
    fields = list(source_fields)
    for extra in ("final_qualification", "outreach_status"):
        if extra not in fields:
            fields.append(extra)
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(output)

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input_file": str(INPUT_FILE.relative_to(ROOT)),
        "output_file": str(OUTPUT_FILE.relative_to(ROOT)),
        "review_rows": len(rows),
        "review_decision_counts": {key: decision_counts.get(key, 0) for key in sorted(VALID_DECISIONS)},
        "qualified_count": len(output),
        "not_qualified_or_pending_count": len(rows) - len(output),
        "qualification_requirements": [
            "review_decision=PASS",
            "review_content_relevance=PASS",
            "review_creator_identity=PASS",
            "review_contact_source=PASS",
            "review_campaign_fit=PASS",
            "valid explicit email address and HTTP(S) email source URL",
            "unique profile URL",
        ],
        "notes": [
            "No pending or needs-more-evidence rows are counted as qualified.",
            "Email syntax and source presence do not establish mailbox deliverability.",
            "This script does not send outreach.",
        ],
    }
    with SUMMARY_FILE.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    print(f"Review rows: {len(rows)}")
    print("Decisions: " + ", ".join(f"{key}={summary['review_decision_counts'][key]}" for key in sorted(VALID_DECISIONS)))
    print(f"Final qualified influencers: {len(output)}")
    print(f"CSV saved to: {OUTPUT_FILE}")
    print(f"Summary saved to: {SUMMARY_FILE}")


if __name__ == "__main__":
    main()
