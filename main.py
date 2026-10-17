"""
Zillow listing scraper (Kansas City, MO) using the Crawio API.

Extracts per listing: price, beds, baths, sqft, listing type, address, URL, photo.
Handles pagination by updating `currentPage` inside Zillow's searchQueryState.

Install:  pip install requests beautifulsoup4
Run:      python zillow_scraper.py
Output:   zillow_listings.json and zillow_listings.csv
"""

import csv
import json
import os
import re
import time
from urllib.parse import quote, unquote

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CRAWIO_API_KEY = os.getenv("CRAWIO_API_KEY", "PASTE_YOUR_CRAWIO_API_KEY_HERE")
CRAWIO_ENDPOINT = "https://api.crawio.com/scrape"

BASE_URL = "https://www.zillow.com/kansas-city-mo/"
SEARCH_QUERY_STATE = {
    "pagination": {},
    "isMapVisible": True,
    "mapBounds": {
        "west": -94.97603383105469,
        "east": -94.17540516894532,
        "south": 38.77407725939054,
        "north": 39.40932406009183,
    },
    "regionSelection": [{"regionId": 18795, "regionType": 6}],
    "filterState": {"sort": {"value": "globalrelevanceex"}},
    "isListVisible": True,
}

MAX_PAGES = 20          # safety cap
DELAY_BETWEEN_PAGES = 2  # seconds
RETRIES = 3


# ---------------------------------------------------------------------------
# URL building / pagination
# ---------------------------------------------------------------------------
def build_page_url(page: int) -> str:
    state = json.loads(json.dumps(SEARCH_QUERY_STATE))  # deep copy
    state["pagination"] = {"currentPage": page} if page > 1 else {}
    encoded = quote(json.dumps(state, separators=(",", ":")), safe="")
    path = BASE_URL if page == 1 else f"{BASE_URL}{page}_p/"
    return f"{path}?category=RECENT_SEARCH&searchQueryState={encoded}"


# ---------------------------------------------------------------------------
# Crawio fetch
# ---------------------------------------------------------------------------
def _extract_html(resp: requests.Response) -> str:
    """Crawio may return raw HTML or JSON wrapping the HTML; handle both."""
    text = resp.text
    try:
        data = resp.json()
    except ValueError:
        return text

    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("html", "content", "body", "data", "result", "response"):
            val = data.get(key)
            if isinstance(val, str) and "<" in val:
                return val
            if isinstance(val, dict):
                for sub in ("html", "content", "body"):
                    if isinstance(val.get(sub), str):
                        return val[sub]
    return text


def fetch_page(url: str) -> str:
    payload = {"url": url, "method": "GET"}
    headers = {"x-api-key": CRAWIO_API_KEY}

    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.post(CRAWIO_ENDPOINT, headers=headers, json=payload, timeout=200)
            resp.raise_for_status()
            return _extract_html(resp)
        except requests.RequestException as e:
            last_err = e
            print(f"  attempt {attempt}/{RETRIES} failed: {e}")
            time.sleep(2 * attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _text(el):
    return el.get_text(" ", strip=True) if el else None


def parse_card(card) -> dict:
    """Parse one property card using stable data-* attributes (not hashed srp-* classes)."""
    # Price
    price_el = card.select_one('[data-test="property-card-price"]')
    if not price_el:
        price_el = card.find("span", string=re.compile(r"^\$[\d,]+"))
    price = _text(price_el)

    # Beds / baths / sqft
    beds = baths = sqft = None
    details_ul = card.select_one('ul[data-testid="property-card-details"]') or card.select_one(
        'ul[data-c11n-component="PropertyCard.HomeDetails"]'
    )
    if details_ul:
        for li in details_ul.find_all("li"):
            value = _text(li.find("b"))
            unit = _text(li.find("abbr"))
            if not value or not unit:
                continue
            if unit.startswith("bd"):
                beds = value
            elif unit.startswith("ba"):
                baths = value
            elif unit.startswith("sqft"):
                sqft = value

    # Listing type, e.g. "House for sale"
    listing_type = None
    data_area = card.select_one('div[data-c11n-component="PropertyCard.DataArea"]')
    if data_area:
        span = data_area.find("span", recursive=False)
        listing_type = _text(span)

    # Address + URL
    link = card.select_one('a[data-testid="property-card-address-link"]') or card.select_one(
        "a.property-card-link"
    )
    address = _text(link.find("address")) if link and link.find("address") else _text(link)
    url = link.get("href") if link else None
    if url and url.startswith("/"):
        url = "https://www.zillow.com" + url

    # Photo
    photo = None
    photo_div = card.select_one('div[data-c11n-component="PropertyCard.Photo"]')
    img = (photo_div or card).find("img")
    if img:
        photo = img.get("src") or img.get("data-src")
        if photo and photo.startswith("data:"):
            photo = None

    return {
        "price": price,
        "beds": beds,
        "baths": baths,
        "sqft": sqft,
        "listing_type": listing_type,
        "address": address,
        "url": url,
        "photo": photo,
    }


def parse_listings_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select('article[data-test="property-card"]')
    if not cards:
        cards = soup.select('[data-test="property-card"]')
    if not cards:
        # Fallback: any element that contains the address link
        cards = [a.find_parent(["article", "li"]) for a in soup.select('a[data-testid="property-card-address-link"]')]
        cards = [c for c in cards if c is not None]

    results = []
    for card in cards:
        item = parse_card(card)
        if item["address"] or item["url"]:
            results.append(item)
    return results


def parse_next_data(html: str) -> list[dict]:
    """Fallback: Zillow embeds listing JSON in <script id="__NEXT_DATA__"> on some responses."""
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return []
    try:
        data = json.loads(script.string)
        list_results = (
            data["props"]["pageProps"]["searchPageState"]["cat1"]["searchResults"]["listResults"]
        )
    except (KeyError, ValueError, TypeError):
        return []

    out = []
    for r in list_results:
        detail = r.get("detailUrl") or ""
        if detail.startswith("/"):
            detail = "https://www.zillow.com" + detail
        out.append(
            {
                "price": r.get("price"),
                "beds": r.get("beds"),
                "baths": r.get("baths"),
                "sqft": r.get("area"),
                "listing_type": r.get("statusText"),
                "address": r.get("address"),
                "url": detail,
                "photo": r.get("imgSrc"),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def scrape(max_pages: int = MAX_PAGES) -> list[dict]:
    all_items: list[dict] = []
    seen = set()

    for page in range(1, max_pages + 1):
        url = build_page_url(page)
        print(f"Page {page}: fetching...")
        html = fetch_page(url)

        items = parse_listings_html(html) or parse_next_data(html)
        new_items = []
        for it in items:
            key = it["url"] or it["address"]
            if key and key not in seen:
                seen.add(key)
                new_items.append(it)

        print(f"  found {len(items)} listings, {len(new_items)} new")
        if not new_items:
            print("No new listings - stopping pagination.")
            break

        all_items.extend(new_items)
        time.sleep(DELAY_BETWEEN_PAGES)

    return all_items


def save(items: list[dict]):
    with open("zillow_listings.json", "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)

    fields = ["price", "beds", "baths", "sqft", "listing_type", "address", "url", "photo"]
    with open("zillow_listings.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(items)


if __name__ == "__main__":
    listings = scrape()
    save(listings)
    print(f"\nDone. {len(listings)} listings saved to zillow_listings.json / zillow_listings.csv")