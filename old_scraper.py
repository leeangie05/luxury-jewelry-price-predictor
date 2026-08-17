# old_scraper.py
#
# Older version of the eBay fine jewelry scraper kept for reference.
# This file is not used by the current scraper.py and does not need to be
# run for the current scraping pipeline to work.import os

import time
import random
import requests
import re
import pandas as pd
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlunparse, urljoin, urlencode

# ---------- config ----------
BASE = "https://www.ebay.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}
SESSION = requests.Session()

OUT_DIR = "data"
OUT_METALS = os.path.join(OUT_DIR, "ebay_fine_jewelry_metals.csv")
OUT_LISTINGS = os.path.join(OUT_DIR, "ebay_fine_jewelry_listings_sample.csv")

os.makedirs(OUT_DIR, exist_ok=True)

# ---------- pacing ----------
def snooze(base=1.2, spread=1.0):
    """Sleep for base + random[0, spread] seconds."""
    time.sleep(base + random.random() * spread)

# ---------- anti-bot detection ----------
def is_interruption(html: str) -> bool:
    """Detect eBay bot-interstitials."""
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    t = soup.title.get_text(strip=True) if soup.title else ""
    return ("Pardon Our Interruption" in t
            or "site connection is secure" in t
            or "To check if the site connection is secure" in t)

# ---------- utils ----------
def fetch(url, retries=3, backoff=2, referer=None):
    for i in range(retries):
        try:
            headers = HEADERS.copy()
            if referer:
                headers["Referer"] = referer
            r = SESSION.get(url, headers=headers, timeout=25, allow_redirects=True)
            r.raise_for_status()
            html = r.text

            # Interruption detection → backoff and raise
            if is_interruption(html):
                wait = 20 + 8 * i
                print(f"[WARN] Interruption page detected; backing off {wait}s for {url}")
                time.sleep(wait)
                raise requests.RequestException("Interruption/anti-bot page")

            return html
        except requests.RequestException as e:
            if i == retries - 1:
                print(f"[ERROR] fetch failed after {retries} tries: {url} ({e})")
                raise
            delay = backoff * (i + 1) + 0.5 * (i + 1) * random.random()
            print(f"[WARN] fetch retry {i+1}/{retries} in {delay:.1f}s: {url}")
            time.sleep(delay)

def clean_price(text: str):
    if not text:
        return None
    part = text.split(" to ")[0]
    digits = "".join(ch for ch in part if ch.isdigit() or ch == ".")
    try:
        return float(digits) if digits else None
    except ValueError:
        return None

def absolute(href: str, base: str = BASE) -> str:
    return urljoin(base, href)

def force_https(u: str) -> str:
    try:
        p = urlparse(u)
        if p.scheme != "https":
            p = p._replace(scheme="https")
        return urlunparse(p)
    except Exception:
        return u

def clean_metal(m: str) -> str:
    """Strip counts like 'Yellow Gold (10,865)' -> 'Yellow Gold'."""
    return m.split("(")[0].strip()

def srp_url_for(brand: str, metal: str) -> str:
    """Build a reliable SRP URL in Fine Jewelry (4196) for brand+metal."""
    q = f'{brand} "{metal}"'
    params = {
        "_sacat": "4196",
        "_nkw": q,
        "rt": "nc",
        "_ipg": "120",
    }
    return f"{BASE}/sch/i.html?{urlencode(params)}"

# ---------- small persistence helpers ----------
def append_rows(rows, path):
    if not rows:
        return
    df = pd.DataFrame(rows)
    header = not os.path.exists(path)
    df.to_csv(path, mode="a", header=header, index=False)

def already_done(row):
    """Skip a metal if we already scraped some rows for that brand+metal."""
    if not os.path.exists(OUT_LISTINGS):
        return False
    try:
        existing = pd.read_csv(OUT_LISTINGS, usecols=["brand", "metal"], dtype=str)
        m = clean_metal(row["metal"])
        mask = (existing["brand"] == row["brand"]) & (existing["metal"] == m)
        return bool(mask.any())
    except Exception:
        return False

# ---------- step 1: brands ----------
def parse_brand_cards_from_marketing(html: str):
    soup = BeautifulSoup(html, "html.parser")
    brands = []
    hdr = soup.find(lambda t: t and t.name in ("h2", "h3")
                    and "shop top brands" in t.get_text(strip=True).lower())
    if not hdr:
        return brands
    section = hdr.find_next(lambda t: t and t.name in ("div", "section", "ul") and t.select("a[href]"))
    if not section:
        return brands
    for a in section.select("a[href]"):
        href = a.get("href")
        if not href:
            continue
        label = a.get_text(" ", strip=True)
        if not label:
            img = a.find("img")
            if img and img.get("alt"):
                label = img["alt"].strip()
        if not label:
            continue
        low = label.lower()
        if any(bad in low for bad in ("sign in", "register", "shop", "deal", "help", "learn more")):
            continue
        if len(label) > 60:
            continue
        brands.append({"brand": label, "brand_url": force_https(absolute(href))})
    # dedupe
    seen, uniq = set(), []
    for b in brands:
        key = (b["brand"].lower(), b["brand_url"])
        if key not in seen:
            seen.add(key)
            uniq.append(b)
    return uniq

def parse_brands_from_facet(html: str):
    soup = BeautifulSoup(html, "html.parser")
    brand_hdr = soup.find(lambda t: t and t.get_text(strip=True).lower() == "brand")
    if not brand_hdr:
        return []
    container = brand_hdr.find_next("ul") or brand_hdr.find_next(lambda t: t and t.name in ("div", "section"))
    if not container:
        return []
    brands = []
    for a in container.select("a[href]"):
        label = a.get_text(" ", strip=True)
        href = a.get("href")
        if not label or not href:
            continue
        clean_label = label.split("(")[0].strip()
        if not clean_label:
            continue
        brands.append({"brand": clean_label, "brand_url": force_https(absolute(href))})
    # dedupe
    seen, uniq = set(), []
    for b in brands:
        key = (b["brand"].lower(), b["brand_url"])
        if key not in seen:
            seen.add(key)
            uniq.append(b)
    return uniq

# ---------- step 2: metals ----------
def parse_shop_by_metal(html: str, brand_name: str, brand_url: str):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    metal_hdr = soup.find(lambda t: t and t.get_text(strip=True).lower() == "metal")
    if not metal_hdr:
        return rows
    container = metal_hdr.find_next("ul") or metal_hdr.find_next(lambda t: t and t.name in ("div", "section"))
    if not container:
        return rows
    for a in container.select("a[href]"):
        text = a.get_text(" ", strip=True)
        href = a.get("href")
        if not text or not href:
            continue
        low = text.lower()
        if any(w in low for w in ("gold", "silver", "platinum", "palladium", "steel", "titanium", "vermeil", "rhodium")):
            rows.append({
                "brand": brand_name,
                "brand_url": brand_url,
                "metal": text,
                "metal_url": force_https(absolute(href)),
            })
    # dedupe
    seen, uniq = set(), []
    for r in rows:
        key = (r["brand"], r["metal_url"])
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq

# ---------- step 3A: SRP listings (if present) ----------
def parse_listings_page(html: str):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    results = soup.select("#srp-river-results .s-item") or soup.select(".s-item")
    for item in results:
        title_el = item.select_one(".s-item__title")
        link_el  = item.select_one("a.s-item__link")
        price_el = item.select_one(".s-item__price")
        if title_el and "Shop on eBay" in title_el.get_text():
            continue
        title = title_el.get_text(strip=True) if title_el else None
        url   = link_el["href"] if link_el and link_el.has_attr("href") else None
        price_text = price_el.get_text(strip=True) if price_el else None
        price = clean_price(price_text)
        if title and url:
            rows.append({"title": title, "price": price, "price_text": price_text, "url": url})
    next_link = (soup.select_one("a[aria-label='Next page']") or
                 soup.select_one("a.pagination__next") or
                 soup.select_one("a[aria-label='Next']"))
    next_url = absolute(next_link["href"]) if next_link and next_link.has_attr("href") else None
    return rows, next_url

def paginate_listings(start_url: str, max_pages: int = 1, delay: float = 3.5, referer=None):
    url = force_https(start_url)
    all_rows, pages = [], 0
    while url and pages < max_pages:
        html = fetch(url, referer=referer)
        rows, next_url = parse_listings_page(html)
        if not rows and pages == 0:
            debug_page(html, label="first-page-empty")
        all_rows.extend(rows)
        url = force_https(next_url) if next_url else None
        pages += 1
        snooze(delay, 1.8)
    return all_rows

def debug_page(html, label=""):
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else None
    count_items = len(soup.select(".s-item"))
    has_results = bool(soup.select_one("#srp-river-results"))
    print(f"[DEBUG {label}] title={title!r} .s-item={count_items} has_results={has_results}")
    print(html[:400])

# ---------- step 3B: fallback via /itm/ links ----------
def extract_itm_links(html: str, max_links: int = 24):
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for a in soup.select('a[href*="/itm/"]'):
        href = a.get("href")
        if not href:
            continue
        href = force_https(absolute(href))
        if "/itm/" in href:
            links.add(href)
        if len(links) >= max_links:
            break
    return list(links)

def parse_item_page(html: str):
    soup = BeautifulSoup(html, "html.parser")

    title_el = (soup.select_one("h1.x-item-title__mainTitle")
                or soup.select_one("h1.it-ttl")
                or soup.select_one("#itemTitle"))
    title = title_el.get_text(" ", strip=True) if title_el else None
    if title and "Details about" in title:
        title = title.replace("Details about", "").strip(" \xa0")

    price_el = (soup.select_one(".x-price-primary")
                or soup.select_one("#prcIsum")
                or soup.select_one("#mm-saleDscPrc")
                or soup.select_one(".notranslate")
                or soup.find("span", {"itemprop": "price"}))
    price_text = price_el.get_text(strip=True) if price_el else None
    price = clean_price(price_text) if price_text else None

    cond_el = (soup.select_one("#vi-itm-cond") or soup.select_one(".x-item-condition-text"))
    condition = cond_el.get_text(" ", strip=True) if cond_el else None

    specifics = {}
    for row in soup.select("#vi-desc-maincntr table tr, .x-about-this-item table tr, section.x-about-this-item li"):
        txt = row.get_text(" ", strip=True)
        if ":" in txt:
            k, v = txt.split(":", 1)
            specifics[k.strip()] = v.strip()

    metal_val = None
    for key in ("Metal", "metal", "Base Metal"):
        if key in specifics:
            metal_val = specifics[key]
            break

    return {
        "title": title,
        "price": price,
        "price_text": price_text,
        "condition": condition,
        "metal_item_page": metal_val,
    }

def scrape_items_via_itm_links(page_url: str, max_items: int = 12, referer: str = None):
    html = fetch(page_url, referer=referer)
    itm_links = extract_itm_links(html, max_links=max_items)
    rows = []
    for k, link in enumerate(itm_links, 1):
        try:
            item_html = fetch(link, referer=page_url)
            item = parse_item_page(item_html)
            item.update({"url": link})
            rows.append(item)
            snooze(0.9, 0.8)  # gentle pacing between item pages
        except Exception as e:
            print(f"   ! Skipped item {k} due to error: {e}")
    return rows

# ---------- orchestrator ----------
def run():
    seed_marketing = "https://www.ebay.com/b/Fine-Jewelry/4196/bn_2408477"
    seed_srp = "https://www.ebay.com/sch/i.html?_sacat=4196"

    print("Fetching marketing brands page…")
    html = fetch(seed_marketing)
    print("Title:", BeautifulSoup(html, "html.parser").title.get_text(strip=True) if html else None)

    # 1) Brands
    brands = parse_brand_cards_from_marketing(html)
    if not brands:
        print("No brands from marketing page; falling back to SRP facets…")
        html = fetch(seed_srp)
        brands = parse_brands_from_facet(html)

    print("First 5 brands:", [b["brand"] for b in brands[:5]])
    print(f"Found ~{len(brands)} brand entries")

    # ---- TEST LIMITS: keep this small while validating ----
    brands = brands[:2]  # two brands per run

    # 2) Metals per brand
    metal_rows = []
    for i, b in enumerate(brands, 1):
        try:
            print(f"[{i}/{len(brands)}] {b['brand']} -> {b['brand_url']}")
            snooze(3.0, 2.5)
            brand_html = fetch(b["brand_url"], referer=seed_marketing)
            metals = parse_shop_by_metal(brand_html, b["brand"], b["brand_url"])

            # limit metals per brand while testing
            metals = metals[:3]
            print(f"  → {len(metals)} metal options: {[m['metal'] for m in metals]}")
            metal_rows.extend(metals)
            snooze(2.5, 2.0)
        except Exception as e:
            print(f"  ! Skipped {b['brand']} due to error: {e}")

    pd.DataFrame(metal_rows).to_csv(OUT_METALS, index=False)
    print(f"Saved metals index: {len(metal_rows)} rows → {OUT_METALS}")

    # Seed a single fallback row if nothing found
    if not metal_rows:
        metal_rows = [{
            "brand": "Tiffany & Co.",
            "brand_url": "https://www.ebay.com/b/Tiffany-and-Co/bn_21836959",
            "metal": "Sterling Silver",
            "metal_url": srp_url_for("Tiffany & Co.", "Sterling Silver"),
        }]

    # 3) Listings with robust fallbacks + checkpointing
    for j, row in enumerate(metal_rows, 1):
        try:
            metal_clean = clean_metal(row["metal"])

            if already_done(row):
                print(f"[{j}/{len(metal_rows)}] Skip {row['brand']} – {metal_clean} (already scraped)")
                continue

            print(f"[{j}/{len(metal_rows)}] Listings: {row['brand']} – {metal_clean}")
            snooze(4.0, 3.0)

            # Try facet URL first
            listings = paginate_listings(row["metal_url"], max_pages=1, referer=row["brand_url"])

            # Fallback to clean SRP if facet empty
            if not listings:
                fallback = srp_url_for(row["brand"], metal_clean)
                print("  → Facet empty; trying SRP:", fallback)
                listings = paginate_listings(fallback, max_pages=1, referer=row["brand_url"])

            # Final fallback: /itm/ extraction + item-page parsing
            if not listings:
                print("  → SRP also empty; extracting /itm/ links and parsing item pages…")
                fallback = srp_url_for(row["brand"], metal_clean)
                listings = scrape_items_via_itm_links(fallback, max_items=12, referer=row["brand_url"])

            # Attach context + append immediately (checkpoint)
            for r in listings:
                r.update({"brand": row["brand"], "metal": metal_clean, "metal_url": row["metal_url"]})
            append_rows(listings, OUT_LISTINGS)

            print(f"  → appended {len(listings)} products to {OUT_LISTINGS}")
            snooze(5.0, 3.5)  # longer pause between metals
        except Exception as e:
            print(f"  ! Skipped listings for {row['brand']} – {row['metal']}: {e}")

if __name__ == "__main__":
    # Quick sanity test for a single item page:
    try:
        test_item = "https://www.ebay.com/itm/286830144288"
        html = fetch(test_item, referer="https://www.ebay.com/")
        print("[sanity item]", parse_item_page(html))
    except Exception as e:
        print("[sanity item] skipped:", e)

    # Full pipeline:
    run()
