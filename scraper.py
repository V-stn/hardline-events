"""
HARDLINE event scraper.

Combines two sources into one events.json:
  1. djguide.nl  — scraped, genre-searched (hardstyle/hardcore/rawstyle/uptempo/frenchcore)
  2. CURATED_FESTIVALS — hand-maintained list of major festivals (stable, doesn't rely on scraping)

Sources are deduped against each other by fuzzy name match + nearby date, so the same
festival showing up in both doesn't create two cards.

Run via GitHub Actions on a schedule. Designed to fail soft: if one source breaks
(site changes its markup, blocks the request, etc.) the other source still writes.

NOTE: this was written without being able to test against djguide.nl's live HTML
(the site returned a bot-detection block when checked). The parser below uses a
regex against the page's plain text rather than specific CSS classes, which tends
to survive markup changes better — but if it returns zero results after your first
real run, the site's output format has likely changed and the DJGUIDE_PATTERN
regex needs a look. Check the Action's log output for the raw text sample it prints.
"""

import json
import re
import sys
import difflib
from datetime import datetime, date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

OUTPUT_PATH = Path(__file__).parent / "events.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

GENRE_QUERIES = ["hardstyle", "hardcore", "rawstyle", "uptempo", "frenchcore"]

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Matches text like: "SA 19 sepIlluminate The Hardstyle Classics Boatparty. 16:00 Boot Waalkade, Nijmegen NL"
DJGUIDE_PATTERN = re.compile(
    r"[A-Z]{2}\s+(\d{1,2})\s+([a-z]{3})([A-Z][^.]{2,80}?)\.\s+(\d{2}:\d{2})\s+"
    r"([^,]{2,60}),\s+([A-Za-z .'-]{2,40}?)\s+([A-Z]{2})(?=\s|$)"
)

# ---------------------------------------------------------------------------
# Source 1: djguide.nl scrape
# ---------------------------------------------------------------------------

def resolve_year(month: int, day: int, today: date) -> int:
    """djguide's listings don't include a year — infer it: if the month/day
    has already passed this year, assume it's next year's occurrence."""
    candidate = date(today.year, month, day)
    if candidate < today:
        return today.year + 1
    return today.year


def scrape_djguide(genre: str, today: date) -> list[dict]:
    url = f"https://www.djguide.nl/events.p?q={genre}&language=en"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[djguide] request failed for '{genre}': {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text(separator=" ", strip=True)

    events = []
    for m in DJGUIDE_PATTERN.finditer(text):
        day_s, mon_s, name, time_s, venue, city, country = m.groups()
        month = MONTHS.get(mon_s.lower())
        if not month:
            continue
        try:
            year = resolve_year(month, int(day_s), today)
            start = date(year, month, int(day_s)).isoformat()
        except ValueError:
            continue

        events.append({
            "name": name.strip(" -"),
            "venue": venue.strip(),
            "city": city.strip(),
            "country": country.strip(),
            "start": start,
            "end": start,  # djguide search results don't expose multi-day spans; refined below
            "genres": [genre],
            "status": "confirmed",
            "sample": False,
            "note": "",
            "url": url,
            "source": "djguide",
        })

    if not events:
        print(f"[djguide] 0 matches for '{genre}'. Sample of fetched text (first 500 chars):", file=sys.stderr)
        print(text[:500], file=sys.stderr)

    return events


def scrape_all_djguide(today: date) -> list[dict]:
    all_events = []
    for genre in GENRE_QUERIES:
        all_events.extend(scrape_djguide(genre, today))
    return all_events


# ---------------------------------------------------------------------------
# Source 2: curated major festivals (hand-maintained — edit this list directly
# in GitHub's mobile web editor whenever a new date is announced)
# ---------------------------------------------------------------------------

CURATED_FESTIVALS = [
    {
        "name": "Dominator — Fatal Fortune",
        "venue": "E3 Strand", "city": "Eersel", "country": "NL",
        "start": "2026-07-17", "end": "2026-07-18",
        "genres": ["hardcore", "uptempo"],
        "status": "confirmed", "sample": False,
        "note": "20th anniversary, 10 stages",
        "url": "https://dominatorfestival.com", "source": "curated",
    },
    {
        "name": "Defqon.1 — Sacred Oath",
        "venue": "Walibi Holland grounds", "city": "Biddinghuizen", "country": "NL",
        "start": "2026-06-25", "end": "2026-06-28",
        "genres": ["hardstyle", "hardcore"],
        "status": "cancelled", "sample": False,
        "note": "Cancelled by Q-dance after KNMI weather warnings",
        "url": "https://defqon1.com", "source": "curated",
    },
    {
        "name": "Decibel outdoor",
        "venue": "Safaripark Beekse Bergen", "city": "Hilvarenbeek", "country": "NL",
        "start": "2026-08-28", "end": "2026-08-30",
        "genres": ["hardstyle", "hardcore", "rawstyle", "uptempo"],
        "status": "confirmed", "sample": False,
        "note": "300+ artists, 'The Loudest City'",
        "url": "https://www.decibeloutdoor.com", "source": "curated",
    },
    {
        "name": "Masters of Hardcore — Tides of Tyranny",
        "venue": "Brabanthallen", "city": "'s-Hertogenbosch", "country": "NL",
        "start": "2026-03-28", "end": "2026-03-28",
        "genres": ["hardcore", "frenchcore", "uptempo"],
        "status": "confirmed", "sample": False,
        "note": "",
        "url": "https://www.mastersofhardcore.com", "source": "curated",
    },
    # Add more here as you confirm real dates — same shape as above.
]


# ---------------------------------------------------------------------------
# Dedupe + merge
# ---------------------------------------------------------------------------

def normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def same_event(a: dict, b: dict) -> bool:
    if a["start"] != b["start"] and a["end"] != b["start"] and a["start"] != b["end"]:
        return False
    ratio = difflib.SequenceMatcher(None, normalize(a["name"]), normalize(b["name"])).ratio()
    return ratio > 0.6


def merge(a: dict, b: dict) -> dict:
    """Curated entries win on fields; genres/urls get unioned."""
    base, other = (a, b) if a["source"] == "curated" else (b, a)
    merged = dict(base)
    merged["genres"] = sorted(set(a["genres"]) | set(b["genres"]))
    if not merged.get("note") and other.get("note"):
        merged["note"] = other["note"]
    return merged


def dedupe(events: list[dict]) -> list[dict]:
    result: list[dict] = []
    for ev in events:
        match_idx = next((i for i, r in enumerate(result) if same_event(ev, r)), None)
        if match_idx is None:
            result.append(ev)
        else:
            result[match_idx] = merge(result[match_idx], ev)
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    today = date.today()

    scraped = scrape_all_djguide(today)
    print(f"[djguide] total raw matches across all genres: {len(scraped)}")

    combined = dedupe(CURATED_FESTIVALS + scraped)
    combined.sort(key=lambda e: e["start"])

    for i, ev in enumerate(combined):
        ev["id"] = f"{normalize(ev['name'])}-{ev['start']}"
        ev.pop("source", None)

    OUTPUT_PATH.write_text(json.dumps(combined, indent=2, ensure_ascii=False))
    print(f"Wrote {len(combined)} events to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
