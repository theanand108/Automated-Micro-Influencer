"""Find explicitly published contact emails on creator-linked public websites.

Run from the repository root:
    python scripts/discover_public_contacts.py

Input:
    data/feasibility/enriched_influencers.csv

Outputs:
    data/feasibility/public_contact_discovery.csv
    data/feasibility/public_contact_discovery_summary.json

Safety and data-quality limits:
- Only visits external website URLs already found in a creator's YouTube description.
- Requests HTML pages only; no login, paywall, CAPTCHA, or access-control bypass.
- Checks DNS results and refuses private/reserved IP addresses.
- Does not follow redirects; redirected URLs need manual review.
- Limits pages per creator and uses request timeouts.
- Records only email strings explicitly present in page text or mailto links.
- Does not infer addresses or claim email deliverability/creator identity.
"""
import csv
import html
import ipaddress
import json
import re
import socket
import time
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

ROOT = Path(__file__).resolve().parents[1]
INPUT_FILE = ROOT / "data" / "feasibility" / "enriched_influencers.csv"
OUTPUT_FILE = ROOT / "data" / "feasibility" / "public_contact_discovery.csv"
SUMMARY_FILE = ROOT / "data" / "feasibility" / "public_contact_discovery_summary.json"

EMAIL_RE = re.compile(
    r"(?<![\\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}(?![\\w.-])",
    re.I,
)
CONTACT_WORDS = ("contact", "about", "business", "collab", "partnership", "press", "work-with")
MAX_PAGES_PER_CREATOR = 3
TIMEOUT_SECONDS = 8
MAX_RESPONSE_BYTES = 1_500_000
REQUEST_DELAY_SECONDS = 0.5
USER_AGENT = "EDXSO-AssignmentContactResearch/1.0 (public contact discovery; limited requests)"


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text_parts = []
        self.links = []
        self._anchor_href = None
        self._anchor_text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == "a" and attrs.get("href"):
            self._anchor_href = attrs["href"].strip()
            self._anchor_text = []
        if tag.lower() == "br":
            self.text_parts.append(" ")

    def handle_data(self, data):
        if data:
            self.text_parts.append(data)
            if self._anchor_href is not None:
                self._anchor_text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._anchor_href is not None:
            self.links.append((self._anchor_href, " ".join(self._anchor_text).strip()))
            self._anchor_href = None
            self._anchor_text = []


def read_rows(path):
    if not path.exists():
        raise FileNotFoundError(
            f"Missing input file: {path}\nRun scripts/enrich_candidates.py first."
        )
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def normalize_url(raw):
    value = html.unescape((raw or "").strip()).rstrip(").,;!?]}>")
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return value


def is_public_host(hostname):
    try:
        literal = ipaddress.ip_address(hostname)
        return literal.is_global
    except ValueError:
        pass
    try:
        records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror):
        return False
    if not records:
        return False
    for record in records:
        address = ipaddress.ip_address(record[4][0].split("%")[0])
        if not address.is_global:
            return False
    return True


def same_host(url, root_host):
    try:
        return (urlparse(url).hostname or "").casefold() == root_host.casefold()
    except ValueError:
        return False


def fetch_html(session, url):
    parsed = urlparse(url)
    if not parsed.hostname or not is_public_host(parsed.hostname):
        return None, "BLOCKED_NON_PUBLIC_OR_UNRESOLVED_HOST"
    try:
        response = session.get(
            url,
            timeout=TIMEOUT_SECONDS,
            allow_redirects=False,
            stream=True,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        )
    except requests.RequestException as exc:
        return None, f"REQUEST_ERROR: {type(exc).__name__}"

    try:
        if 300 <= response.status_code < 400:
            return None, f"REDIRECT_NOT_FOLLOWED: HTTP_{response.status_code}"
        if response.status_code != 200:
            return None, f"HTTP_{response.status_code}"
        content_type = response.headers.get("Content-Type", "").casefold()
        if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
            return None, "SKIPPED_NON_HTML_CONTENT"
        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=16384):
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                return None, "SKIPPED_RESPONSE_TOO_LARGE"
            chunks.append(chunk)
        encoding = response.encoding or "utf-8"
        return b"".join(chunks).decode(encoding, errors="replace"), "OK"
    finally:
        response.close()


def parse_page(page_text):
    parser = PageParser()
    parser.feed(page_text)
    text = html.unescape(" ".join(parser.text_parts))
    emails = set(EMAIL_RE.findall(text))
    for href, _label in parser.links:
        decoded = html.unescape(href.strip())
        if decoded.casefold().startswith("mailto:"):
            address = decoded[7:].split("?", 1)[0].strip()
            if EMAIL_RE.fullmatch(address):
                emails.add(address)
    return parser, emails


def candidate_pages(start_url, session, max_pages=MAX_PAGES_PER_CREATOR):
    """Fetch a linked homepage and up to two obvious same-host contact/about pages."""
    parsed_start = urlparse(start_url)
    root_host = (parsed_start.hostname or "").casefold()
    queue = [start_url]
    visited = set()
    observations = []
    while queue and len(visited) < max_pages:
        url = queue.pop(0)
        if url in visited or not same_host(url, root_host):
            continue
        visited.add(url)
        page_text, status = fetch_html(session, url)
        if status != "OK" or page_text is None:
            observations.append((url, status, set()))
            time.sleep(REQUEST_DELAY_SECONDS)
            continue
        parser, emails = parse_page(page_text)
        observations.append((url, status, emails))
        for href, label in parser.links:
            joined = normalize_url(urljoin(url, html.unescape(href)))
            if not joined or not same_host(joined, root_host):
                continue
            signal = f"{label} {urlparse(joined).path}".casefold()
            if any(word in signal for word in CONTACT_WORDS) and joined not in visited and joined not in queue:
                queue.append(joined)
        time.sleep(REQUEST_DELAY_SECONDS)
    return observations


def main():
    rows = read_rows(INPUT_FILE)
    output = []
    counts = Counter()
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    for row in rows:
        if row.get("subscriber_range_filter") != "PASS":
            continue
        name = row.get("influencer_name", "")
        profile_url = row.get("profile_url", "")
        raw_sites = row.get("external_website_candidates", "")
        sites = []
        if raw_sites and raw_sites != "Not Found":
            for raw_site in raw_sites.split(";"):
                normalized_url = normalize_url(raw_site)
                if normalized_url and normalized_url not in sites:
                    sites.append(normalized_url)

        creator_emails = {}
        attempted_urls = set()
        site_statuses = []
        for site in sites:
            remaining_pages = MAX_PAGES_PER_CREATOR - len(attempted_urls)
            if remaining_pages <= 0:
                break
            for page_url, status, emails in candidate_pages(site, session, remaining_pages):
                attempted_urls.add(page_url)
                site_statuses.append(f"{page_url} [{status}]")
                for email in sorted(emails, key=str.casefold):
                    creator_emails.setdefault(email.casefold(), (email, page_url))
            if len(attempted_urls) >= MAX_PAGES_PER_CREATOR:
                break

        if not sites:
            counts["creators_without_website_candidates"] += 1
            output.append({
                "influencer_name": name,
                "youtube_profile_url": profile_url,
                "website_candidate": "Not Found",
                "page_checked": "Not Checked",
                "public_email_candidate": "Not Found",
                "email_source_url": "Not Found",
                "discovery_status": "NO_EXTERNAL_WEBSITE_CANDIDATE",
                "verification_status": "NOT VERIFIED",
                "notes": "No external website URL was present in the available channel description.",
            })
            continue

        if creator_emails:
            counts["creators_with_email_candidates"] += 1
        else:
            counts["creators_without_email_candidates"] += 1
        for email, source_url in sorted(creator_emails.values(), key=lambda pair: pair[0].casefold()):
            output.append({
                "influencer_name": name,
                "youtube_profile_url": profile_url,
                "website_candidate": "; ".join(sites),
                "page_checked": "; ".join(sorted(attempted_urls)),
                "public_email_candidate": email,
                "email_source_url": source_url,
                "discovery_status": "EMAIL_PATTERN_FOUND_ON_PUBLIC_PAGE",
                "verification_status": "UNVERIFIED — CONFIRM CREATOR IDENTITY AND SOURCE",
                "notes": "Explicitly published page text/mailto candidate; deliverability not tested.",
            })
        if not creator_emails:
            output.append({
                "influencer_name": name,
                "youtube_profile_url": profile_url,
                "website_candidate": "; ".join(sites),
                "page_checked": "; ".join(sorted(attempted_urls)) or "None",
                "public_email_candidate": "Not Found",
                "email_source_url": "Not Found",
                "discovery_status": "NO_EMAIL_PATTERN_FOUND",
                "verification_status": "NOT VERIFIED",
                "notes": "; ".join(site_statuses) or "No page was successfully inspected.",
            })

    fields = [
        "influencer_name", "youtube_profile_url", "website_candidate", "page_checked",
        "public_email_candidate", "email_source_url", "discovery_status",
        "verification_status", "notes",
    ]
    with OUTPUT_FILE.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(output)

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "size_eligible_creators_processed": sum(
            1 for row in rows if row.get("subscriber_range_filter") == "PASS"
        ),
        "creators_with_email_candidates_found_on_public_websites": counts["creators_with_email_candidates"],
        "creators_with_no_email_candidate_found_on_checked_pages": counts["creators_without_email_candidates"],
        "creators_without_external_website_candidates": counts["creators_without_website_candidates"],
        "email_addresses_found": sum(
            1 for item in output if item["public_email_candidate"] != "Not Found"
        ),
        "final_qualified_influencers": 0,
        "limitations": [
            "Only creator-linked external website URLs already present in the local enrichment CSV were inspected.",
            "A maximum of three pages was inspected per creator; no redirect was followed.",
            "Email pattern discovery does not verify ownership, currentness, consent, or deliverability.",
            "Manual review is required before adding a contact to the outreach list.",
        ],
        "output_file": str(OUTPUT_FILE.relative_to(ROOT)),
    }
    with SUMMARY_FILE.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nContact discovery CSV saved to: {OUTPUT_FILE}")
    print(f"Summary saved to: {SUMMARY_FILE}")


if __name__ == "__main__":
    main()
