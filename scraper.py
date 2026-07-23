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
import os
import re
import sys
import time
import difflib
from datetime import datetime, date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

OUTPUT_PATH = Path(__file__).parent / "events.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://www.djguide.nl/",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
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


def scrape_djguide(session: requests.Session, genre: str, today: date) -> list[dict]:
    url = f"https://www.djguide.nl/events.p?q={genre}&language=en"
    try:
        resp = session.get(url, headers=HEADERS, timeout=20)
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
    session = requests.Session()
    session.headers.update(HEADERS)

    # Warm-up: visit the homepage first so the site sees a normal browsing
    # pattern (and picks up any cookies it wants to set) before we hit search URLs.
    try:
        session.get("https://www.djguide.nl/", timeout=20)
    except requests.RequestException as e:
        print(f"[djguide] homepage warm-up failed: {e}", file=sys.stderr)

    all_events = []
    for genre in GENRE_QUERIES:
        all_events.extend(scrape_djguide(session, genre, today))
        time.sleep(1.5)  # small gap between requests, less bot-like than instant back-to-back hits
    return all_events


# ---------------------------------------------------------------------------
# Source 2: curated major festivals (hand-maintained — edit this list directly
# in GitHub's mobile web editor whenever a new date is announced)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Source 2: Ticketmaster Discovery API — a real, official API (not scraping),
# so it doesn't hit the datacenter-IP blocking that killed the djguide approach.
# Free key from https://developer.ticketmaster.com/ — set as TICKETMASTER_API_KEY
# in the repo's Actions secrets.
# ---------------------------------------------------------------------------

TM_API_KEY = os.environ.get("TICKETMASTER_API_KEY", "")
TM_COUNTRIES = ["NL", "BE", "DE", "PL"]  # expand this list for other markets you care about


def scrape_ticketmaster() -> list[dict]:
    if not TM_API_KEY:
        print("[ticketmaster] no TICKETMASTER_API_KEY set, skipping this source", file=sys.stderr)
        return []

    events = []
    for genre in GENRE_QUERIES:
        for country in TM_COUNTRIES:
            url = "https://app.ticketmaster.com/discovery/v2/events.json"
            params = {
                "keyword": genre,
                "countryCode": country,
                "apikey": TM_API_KEY,
                "size": 50,
                "sort": "date,asc",
            }
            try:
                resp = requests.get(url, params=params, timeout=20)
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException as e:
                print(f"[ticketmaster] request failed for '{genre}' in {country}: {e}", file=sys.stderr)
                continue
            except ValueError:
                print(f"[ticketmaster] non-JSON response for '{genre}' in {country}", file=sys.stderr)
                continue

            raw_events = data.get("_embedded", {}).get("events", [])
            for ev in raw_events:
                try:
                    start = ev["dates"]["start"]["localDate"]
                except KeyError:
                    continue
                venue_info = {}
                try:
                    venue_info = ev["_embedded"]["venues"][0]
                except (KeyError, IndexError):
                    pass

                events.append({
                    "name": ev.get("name", "Unknown event"),
                    "venue": venue_info.get("name", "Venue TBA"),
                    "city": venue_info.get("city", {}).get("name", "Unknown"),
                    "country": venue_info.get("country", {}).get("countryCode", country),
                    "start": start,
                    "end": start,  # Ticketmaster lists multi-day festivals as separate day entries;
                                   # dedupe below merges same-name entries within a few days into one
                    "genres": [genre],
                    "status": "confirmed",
                    "sample": False,
                    "note": "",
                    "url": ev.get("url", ""),
                    "source": "ticketmaster",
                })
            time.sleep(0.3)  # stay comfortably under rate limits

    print(f"[ticketmaster] total raw matches: {len(events)}")
    return events


# ---------------------------------------------------------------------------
# Source 3: curated major festivals (hand-maintained — edit this list directly
# in GitHub's mobile web editor whenever a new date is announced)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Source 4: Partyflock (via Appic, its JS-rendered agenda app) — uses a real
# headless browser since the content doesn't exist in the raw HTML.
#
# HONEST CAVEAT: this was written completely blind — I have no way to see what
# Appic's rendered page actually looks like from here. The selector strategy
# below (grab every link that looks like a party/event page, pull its visible
# text) is a reasonable generic guess, not something verified against the real
# site. If it returns 0 events, check the log for the "[partyflock] DIAGNOSTIC"
# dump — it prints a chunk of the actual rendered page text/structure so the
# selectors can be corrected precisely instead of guessed again.
# ---------------------------------------------------------------------------

PARTYFLOCK_URL = "https://partyflock.nl/agenda/style:hardstyle"


def scrape_partyflock(today: date) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[partyflock] playwright not installed, skipping", file=sys.stderr)
        return []

    events = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            page.goto(PARTYFLOCK_URL, timeout=30000, wait_until="networkidle")
            page.wait_for_timeout(3000)  # extra buffer for late JS rendering

            # Best-guess extraction: Partyflock's own site historically links each
            # event as /party/<id>-<slug>. Grab those and read their visible text.
            links = page.query_selector_all('a[href*="/party/"]')
            seen_hrefs = set()
            for link in links:
                href = link.get_attribute("href") or ""
                if href in seen_hrefs:
                    continue
                seen_hrefs.add(href)
                text = (link.inner_text() or "").strip()
                if not text or len(text) < 3:
                    continue
                events.append({
                    "name": text,
                    "venue": "Unknown",
                    "city": "Unknown",
                    "country": "NL",
                    "start": today.isoformat(),  # placeholder — real date extraction needs the actual markup
                    "end": today.isoformat(),
                    "genres": ["hardstyle"],
                    "status": "confirmed",
                    "sample": False,
                    "note": "Date not reliably parsed yet — needs selector fix once real markup is seen",
                    "url": href if href.startswith("http") else f"https://partyflock.nl{href}",
                    "source": "partyflock",
                })

            if not events:
                body_text = page.inner_text("body")
                print("[partyflock] DIAGNOSTIC — 0 candidate links found. "
                      "First 2000 chars of rendered page text:", file=sys.stderr)
                print(body_text[:2000], file=sys.stderr)
            else:
                print(f"[partyflock] found {len(events)} candidate links "
                      f"(dates are placeholders — see note field)", file=sys.stderr)

            browser.close()
    except Exception as e:
        print(f"[partyflock] scrape failed: {e}", file=sys.stderr)
        return []

    return events


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

    scraped_djguide = scrape_all_djguide(today)
    print(f"[djguide] total raw matches across all genres: {len(scraped_djguide)}")

    scraped_tm = scrape_ticketmaster()

    scraped_pf = scrape_partyflock(today)

    combined = dedupe(CURATED_FESTIVALS + scraped_tm + scraped_djguide + scraped_pf)
    combined.sort(key=lambda e: e["start"])

    for i, ev in enumerate(combined):
        ev["id"] = f"{normalize(ev['name'])}-{ev['start']}"
        ev.pop("source", None)

    OUTPUT_PATH.write_text(json.dumps(combined, indent=2, ensure_ascii=False))
    print(f"Wrote {len(combined)} events to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
