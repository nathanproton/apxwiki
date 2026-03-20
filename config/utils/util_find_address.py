#!/usr/bin/env python3
"""
util_find_address.py — ApxWiki Address Finder Utility

Scans ApxWiki HTML article pages for likely street/mailing addresses
(e.g., "123 Sesame Street", "P.O. Box 456") and reports each occurrence
with its file location, page type, and the flagged text in context.

Designed primarily to audit biography pages for inadvertently published
home addresses, but works across all article types.

Usage:
    # From the apxwiki/ directory:
    python3 config/utils/util_find_address.py                  # scan all pages
    python3 config/utils/util_find_address.py --type biography  # biographies only
    python3 config/utils/util_find_address.py --file Some_Page.html  # one file
    python3 config/utils/util_find_address.py --verbose         # show per-file progress
    python3 config/utils/util_find_address.py --json            # machine-readable output
"""

import argparse
import html
import json
import os
import re
import sys
from pathlib import Path

# ── Resolve paths ───────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent          # config/utils/
CONFIG_DIR = SCRIPT_DIR.parent                         # config/
APXWIKI_DIR = CONFIG_DIR.parent                        # apxwiki/
PAGES_JSON = CONFIG_DIR / "pages.json"

# ── Address regex patterns ──────────────────────────────────────────
# These are intentionally broad to catch as many real-world address
# formats as possible, at the cost of some false positives (which is
# the right trade-off for a privacy audit tool).

# Common street type suffixes (full + abbreviated)
_STREET_TYPES = (
    r"(?:"
    r"Street|St\.?|Avenue|Ave\.?|Boulevard|Blvd\.?|Drive|Dr\.?|"
    r"Road|Rd\.?|Lane|Ln\.?|Court|Ct\.?|Circle|Cir\.?|"
    r"Place|Pl\.?|Way|Terrace|Ter\.?|Trail|Trl\.?|"
    r"Parkway|Pkwy\.?|Pike|Highway|Hwy\.?|Route|Rte\.?|"
    r"Loop|Run|Path|Crossing|Xing|Ridge|Cove|"
    r"Alley|Aly\.?|Bend|Bluff|Branch|Creek|Estates|"
    r"Fork|Glen|Heights|Hts\.?|Hill|Hollow|Knoll|"
    r"Landing|Manor|Meadow|Mill|Oaks|Overlook|Park|"
    r"Point|Pt\.?|Springs|Square|Sq\.?|Summit|Valley|View|"
    r"Vista|Walk|Woods"
    r")"
)

# Pattern 1: Classic street address  —  "123 Main Street"
#   number + optional direction + one or more name words + street type
#   optional apt/suite/unit suffix
_DIRECTION = r"(?:N\.?|S\.?|E\.?|W\.?|North|South|East|West|NE|NW|SE|SW)"
_APT_SUFFIX = r"(?:\s*[,.]?\s*(?:Apt\.?|Suite|Ste\.?|Unit|#|Lot|Bldg\.?|Building)\s*[#]?\s*[\w\-]+)?"

_PAT_STREET = re.compile(
    r"\b"
    r"(\d{1,6})"                             # house number
    r"\s+"
    rf"(?:{_DIRECTION}\s+)?"                  # optional directional
    r"([A-Z][A-Za-z''\-]+)"                   # first word of street name (capitalized)
    r"(?:\s+[A-Z][A-Za-z''\-]+){0,3}"        # up to 3 more name words
    r"\s+"
    rf"{_STREET_TYPES}"                       # street type
    rf"{_APT_SUFFIX}"                         # optional apartment/suite
    r"\b",
    re.UNICODE
)

# Pattern 2: P.O. Box
_PAT_PO_BOX = re.compile(
    r"\b"
    r"(?:P\.?\s*O\.?\s*Box|Post\s+Office\s+Box)"
    r"\s+"
    r"\d{1,6}"
    r"\b",
    re.IGNORECASE
)

# Pattern 3: Rural route / county route
_PAT_RURAL = re.compile(
    r"\b"
    r"(?:R\.?R\.?|Rural\s+Route|County\s+Road|CR|State\s+Road|SR)"
    r"\s*#?\s*"
    r"\d{1,5}"
    r"(?:\s*,?\s*Box\s+\d{1,5})?"
    r"\b",
    re.IGNORECASE
)

# Pattern 4: Virginia route style  —  "Route 24" / "Rt. 460"
_PAT_VA_ROUTE = re.compile(
    r"\b"
    r"(?:Route|Rt\.?|Rte\.?)"
    r"\s+"
    r"\d{1,4}"
    r"\b",
    re.IGNORECASE
)

# Pattern 5: Number + named road without a standard suffix
#   Catches things like "1234 Oakville Road" where "Road" is the suffix,
#   but also "456 Confederate Boulevard Extension" etc.
#   This is the loosest pattern — higher false-positive rate.
_PAT_NUMBER_ROAD = re.compile(
    r"\b"
    r"(\d{1,6})"
    r"\s+"
    r"([A-Z][A-Za-z''\-]+(?:\s+[A-Z][A-Za-z''\-]+){0,4})"
    r"\s+"
    r"(?:Road|Rd|Lane|Ln|Drive|Dr|Street|St|Avenue|Ave)"
    r"\.?"
    r"\b"
)

ALL_PATTERNS = [
    ("street_address", _PAT_STREET),
    ("po_box",         _PAT_PO_BOX),
    ("rural_route",    _PAT_RURAL),
    ("va_route",       _PAT_VA_ROUTE),
    ("number_road",    _PAT_NUMBER_ROAD),
]

# ── Zones to exclude (nav, footer, refs, CSS, meta) ────────────────
# We only want to flag addresses in the article *body* content, not in
# boilerplate nav links, footers, reference citations, CSS, or meta tags.
_STRIP_ZONES = [
    (re.compile(r"<style[\s>].*?</style>",      re.S | re.I), ""),
    (re.compile(r"<script[\s>].*?</script>",     re.S | re.I), ""),
    (re.compile(r"<!--.*?-->",                   re.S),        ""),
    (re.compile(r'<meta[^>]*>',                  re.I),        ""),
    (re.compile(r'<div id="apxwiki-nav">.*?</div>\s*(?=<div id="content">)', re.S | re.I), ""),
    (re.compile(r'<div id="apxwiki-footer">.*?</div>',                      re.S | re.I), ""),
]


# ── Helpers ─────────────────────────────────────────────────────────

def load_pages_registry() -> dict:
    """Load pages.json and return a dict mapping filename → page metadata."""
    if not PAGES_JSON.exists():
        print(f"WARNING: pages.json not found at {PAGES_JSON}", file=sys.stderr)
        return {}
    with open(PAGES_JSON, "r", encoding="utf-8") as f:
        data = json.load(f)
    registry = {}
    for page in data.get("pages", []):
        registry[page["filename"]] = page
    return registry


def strip_html_tags(text: str) -> str:
    """Remove HTML tags but preserve the text content."""
    return re.sub(r"<[^>]+>", " ", text)


def decode_entities(text: str) -> str:
    """Decode HTML entities (e.g., &amp; → &)."""
    return html.unescape(text)


def extract_body_text(raw_html: str) -> str:
    """
    Extract only the article body text from an ApxWiki HTML page,
    stripping nav, footer, CSS, comments, and HTML tags.
    Returns plain text suitable for address scanning.
    """
    text = raw_html
    for pattern, replacement in _STRIP_ZONES:
        text = pattern.sub(replacement, text)
    text = strip_html_tags(text)
    text = decode_entities(text)
    # Collapse whitespace but preserve line breaks for location tracking
    text = re.sub(r"[ \t]+", " ", text)
    return text


def get_context(text: str, start: int, end: int, ctx_chars: int = 60) -> str:
    """Return the matched text with surrounding context characters."""
    ctx_start = max(0, start - ctx_chars)
    ctx_end = min(len(text), end + ctx_chars)
    before = text[ctx_start:start].lstrip()
    matched = text[start:end]
    after = text[end:ctx_end].rstrip()
    return f"...{before}>>>{matched}<<<{after}..."


def find_addresses_in_text(text: str) -> list[dict]:
    """
    Run all address patterns against the given text.
    Returns a list of match dicts with pattern_name, matched_text,
    start/end positions, and surrounding context.
    """
    results = []
    seen_spans = set()  # deduplicate overlapping matches

    for pattern_name, pattern in ALL_PATTERNS:
        for m in pattern.finditer(text):
            span = (m.start(), m.end())
            # Skip if this span is a subset of or overlaps with an existing match
            is_dup = False
            for s_start, s_end in seen_spans:
                if m.start() >= s_start and m.end() <= s_end:
                    is_dup = True
                    break
            if is_dup:
                continue
            seen_spans.add(span)

            matched_text = m.group(0).strip()
            context = get_context(text, m.start(), m.end())

            results.append({
                "pattern": pattern_name,
                "matched_text": matched_text,
                "start": m.start(),
                "end": m.end(),
                "context": context,
            })

    # Sort by position in the document
    results.sort(key=lambda r: r["start"])
    return results


def approximate_line_number(raw_html: str, body_text: str, char_pos: int) -> int:
    """
    Rough estimate of the line number in the original HTML file
    corresponding to a character position in the extracted body text.
    Not exact (because of tag stripping) but useful for locating content.
    """
    # Find the matched text snippet
    snippet = body_text[char_pos:char_pos + 30]
    # Search for it in the raw HTML
    idx = raw_html.find(snippet)
    if idx == -1:
        # Try a shorter snippet
        snippet = body_text[char_pos:char_pos + 15]
        idx = raw_html.find(snippet)
    if idx == -1:
        return -1
    return raw_html[:idx].count("\n") + 1


# ── Exclusion heuristics ───────────────────────────────────────────
# Some matches are clearly not personal home addresses. We flag them
# with lower severity or skip them.

# Known institutional / government addresses that are fine to publish
_KNOWN_SAFE_FRAGMENTS = [
    "court house",
    "courthouse",
    "town hall",
    "post office",
    "p.o. box",
    "po box",
    "fire station",
    "fire department",
    "rescue squad",
    "library",
    "school",
    "church",
    "cemetery",
    "hospital",
    "medical center",
    "health department",
    "sheriff",
    "police",
    "jail",
    "prison",
    "government center",
    "civic center",
    "community center",
    "recreation center",
    "national park",
    "monument",
    "battlefield",
    "museum",
    "bank",
    "landfill",
    "transfer station",
    "wastewater",
    "treatment plant",
    "water plant",
    "pump station",
    "tower",
    "substation",
]


def classify_match(match: dict, page_type: str) -> dict:
    """
    Classify a match as 'flag' (needs review), 'institutional' (likely
    safe — a public/government building), or 'route_ref' (just a road
    name reference, not a personal address).

    Adds 'severity' and 'classification' keys to the match dict.
    """
    text_lower = match["matched_text"].lower()
    ctx_lower = match["context"].lower()

    # Check for known institutional keywords in the match or context
    for frag in _KNOWN_SAFE_FRAGMENTS:
        if frag in text_lower or frag in ctx_lower:
            match["classification"] = "institutional"
            match["severity"] = "low"
            return match

    # Route references without a house number are informational, not addresses
    if match["pattern"] == "va_route":
        # Only flag if preceded by a house number in context
        preceding = match["context"][:60].strip()
        if not re.search(r"\d{1,6}\s*$", preceding):
            match["classification"] = "route_reference"
            match["severity"] = "info"
            return match

    # Biographies get higher severity — these are the privacy concern
    if page_type == "biography":
        match["classification"] = "potential_home_address"
        match["severity"] = "high"
    else:
        match["classification"] = "address_mention"
        match["severity"] = "medium"

    return match


# ── Main scan logic ─────────────────────────────────────────────────

def scan_file(filepath: Path, page_meta: dict | None) -> list[dict]:
    """
    Scan a single HTML file for address occurrences.
    Returns a list of finding dicts.
    """
    raw_html = filepath.read_text(encoding="utf-8", errors="replace")
    body_text = extract_body_text(raw_html)

    page_type = page_meta.get("type", "unknown") if page_meta else "unknown"
    page_title = page_meta.get("title", filepath.stem.replace("_", " ")) if page_meta else filepath.stem.replace("_", " ")

    matches = find_addresses_in_text(body_text)
    findings = []
    for m in matches:
        m = classify_match(m, page_type)
        m["filename"] = filepath.name
        m["page_title"] = page_title
        m["page_type"] = page_type
        m["approx_line"] = approximate_line_number(raw_html, body_text, m["start"])
        findings.append(m)

    return findings


def main():
    parser = argparse.ArgumentParser(
        description="Scan ApxWiki articles for likely street addresses."
    )
    parser.add_argument(
        "--type", "-t",
        help="Filter to a specific page type (e.g., biography, organization).",
        default=None,
    )
    parser.add_argument(
        "--file", "-f",
        help="Scan a single file by filename (e.g., John_Doe.html).",
        default=None,
    )
    parser.add_argument(
        "--severity", "-s",
        help="Minimum severity to show: info, low, medium, high (default: low).",
        default="low",
        choices=["info", "low", "medium", "high"],
    )
    parser.add_argument(
        "--json", "-j",
        action="store_true",
        help="Output results as JSON.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show per-file progress on stderr.",
    )
    args = parser.parse_args()

    severity_order = {"info": 0, "low": 1, "medium": 2, "high": 3}
    min_severity = severity_order.get(args.severity, 1)

    # Load page registry
    registry = load_pages_registry()

    # Determine which files to scan
    if args.file:
        target = APXWIKI_DIR / args.file
        if not target.exists():
            print(f"ERROR: File not found: {target}", file=sys.stderr)
            sys.exit(1)
        files = [target]
    else:
        files = sorted(APXWIKI_DIR.glob("*.html"))
        # Exclude index.html
        files = [f for f in files if f.name != "index.html"]

    # Filter by type if requested
    if args.type:
        files = [
            f for f in files
            if registry.get(f.name, {}).get("type") == args.type
        ]

    all_findings = []
    files_scanned = 0
    files_with_hits = 0

    for filepath in files:
        meta = registry.get(filepath.name)
        if args.verbose:
            print(f"  Scanning {filepath.name}...", file=sys.stderr)
        findings = scan_file(filepath, meta)
        # Filter by severity
        findings = [
            f for f in findings
            if severity_order.get(f["severity"], 0) >= min_severity
        ]
        if findings:
            files_with_hits += 1
        all_findings.extend(findings)
        files_scanned += 1

    # ── Output ──────────────────────────────────────────────────────
    if args.json:
        output = {
            "summary": {
                "files_scanned": files_scanned,
                "files_with_findings": files_with_hits,
                "total_findings": len(all_findings),
                "filter_type": args.type,
                "min_severity": args.severity,
            },
            "findings": all_findings,
        }
        print(json.dumps(output, indent=2))
    else:
        # Human-readable report
        print("=" * 72)
        print("  ApxWiki Address Finder — Scan Report")
        print("=" * 72)
        print(f"  Files scanned:       {files_scanned}")
        print(f"  Files with findings: {files_with_hits}")
        print(f"  Total findings:      {len(all_findings)}")
        if args.type:
            print(f"  Filtered to type:    {args.type}")
        print(f"  Min severity shown:  {args.severity}")
        print("=" * 72)

        if not all_findings:
            print("\n  ✓ No addresses found matching the criteria.\n")
            return

        # Group by file
        by_file: dict[str, list[dict]] = {}
        for f in all_findings:
            by_file.setdefault(f["filename"], []).append(f)

        for filename, findings in sorted(by_file.items()):
            page_type = findings[0]["page_type"]
            page_title = findings[0]["page_title"]
            print(f"\n{'─' * 72}")
            print(f"  📄 {filename}")
            print(f"     Title: {page_title}")
            print(f"     Type:  {page_type}")
            print(f"     Hits:  {len(findings)}")
            print(f"{'─' * 72}")

            for i, f in enumerate(findings, 1):
                sev_icon = {
                    "high": "🔴",
                    "medium": "🟡",
                    "low": "🟢",
                    "info": "ℹ️ ",
                }[f["severity"]]

                print(f"\n  [{i}] {sev_icon} {f['severity'].upper()} — {f['classification']}")
                print(f"      Pattern:  {f['pattern']}")
                print(f"      Match:    \"{f['matched_text']}\"")
                if f["approx_line"] > 0:
                    print(f"      Line:     ~{f['approx_line']}")
                print(f"      Context:  {f['context']}")

        # Summary footer
        high_count = sum(1 for f in all_findings if f["severity"] == "high")
        med_count = sum(1 for f in all_findings if f["severity"] == "medium")
        low_count = sum(1 for f in all_findings if f["severity"] == "low")
        info_count = sum(1 for f in all_findings if f["severity"] == "info")

        print(f"\n{'=' * 72}")
        print(f"  Summary: {high_count} high, {med_count} medium, "
              f"{low_count} low, {info_count} info")
        if high_count > 0:
            print(f"  ⚠️  {high_count} HIGH severity finding(s) in biography pages")
            print(f"     require manual review for possible home addresses.")
        print(f"{'=' * 72}\n")


if __name__ == "__main__":
    main()
