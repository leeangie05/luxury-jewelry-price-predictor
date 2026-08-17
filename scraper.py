# scraper_ebay_fine_jewelry.py
import os
import csv
import time
import random
import argparse
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

# ---------- polite pacing ----------
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

# Static, approximate FX rates to USD. These are NOT live rates — refresh them
# periodically (or swap in a real FX API call) before trusting them for anything
# beyond rough modeling. Kept as a static table so the scraper has no extra
# network dependency.
FX_TO_USD = {
    "US": 1.0,
    "USD": 1.0,
    "GBP": 1.27,
    "EUR": 1.09,
    "CAD": 0.73,
    "AUD": 0.66,
    "CHF": 1.14,
}

def parse_currency(price_text: str):
    """Return (currency_code, price_in_usd) from a raw price string like
    'US $649.99' or 'GBP 121.46'. Falls back to (None, None) if it can't tell."""
    if not price_text:
        return None, None
    price = clean_price(price_text)
    if price is None:
        return None, None
    m = re.match(r"^\s*([A-Za-z]{2,3})", price_text.strip())
    code = m.group(1).upper() if m else None
    if code == "US":
        code = "USD"
    if code in FX_TO_USD:
        return code, round(price * FX_TO_USD[code], 2)
    if price_text.strip().startswith("$"):
        return "USD", price
    return code or "UNKNOWN", None

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

# Canonical listings schema. Parsers may emit a subset (SRP rows have no
# item-page specifics; older files predate currency/price_usd). All writes
# go through this layout so a later scrape never appends a different shape
# onto an existing CSV.
LISTING_COLUMNS = [
    "title",
    "price",
    "price_text",
    "currency",
    "price_usd",
    "condition",
    "metal_item_page",
    "purity_item_page",
    "weight_item_page",
    "length_item_page",
    "url",
    "brand",
    "metal",
    "metal_url",
]

# Historical on-disk layouts, keyed by field count. Used only to interpret
# rows that were appended after a schema change without rewriting the file.
_LISTING_SCHEMAS_BY_WIDTH = {
    9: [
        "title", "price", "price_text", "condition", "metal_item_page",
        "url", "brand", "metal", "metal_url",
    ],
    10: [
        "title", "price", "price_text", "currency", "price_usd",
        "condition", "url", "brand", "metal", "metal_url",
    ],
}


def _blank_mask(series):
    s = series.astype("string")
    return series.isna() | s.str.strip().eq("") | s.str.lower().eq("nan")


def _read_listings_csv(path):
    """Load listings even when old and new row shapes are mixed in one file."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return pd.DataFrame(columns=LISTING_COLUMNS)
        records = []
        for row in reader:
            if not row or all(c.strip() == "" for c in row):
                continue
            n = len(row)
            if n == len(header):
                rec = dict(zip(header, row))
            elif n in _LISTING_SCHEMAS_BY_WIDTH:
                rec = dict(zip(_LISTING_SCHEMAS_BY_WIDTH[n], row))
            else:
                rec = dict(zip(header, row))
            records.append(rec)
    if not records:
        return pd.DataFrame(columns=header or LISTING_COLUMNS)
    return pd.DataFrame.from_records(records)


def _align_listings(df):
    """Reindex to LISTING_COLUMNS, backfilling currency/price_usd when possible."""
    if df is None or df.empty:
        return pd.DataFrame(columns=LISTING_COLUMNS)
    out = df.copy()
    if "currency" not in out.columns:
        out["currency"] = pd.NA
    if "price_usd" not in out.columns:
        out["price_usd"] = pd.NA
    if "price_text" in out.columns:
        missing = _blank_mask(out["currency"])
        if missing.any():
            parsed = out.loc[missing, "price_text"].map(
                lambda t: parse_currency(t) if pd.notna(t) and str(t).strip() else (None, None)
            )
            out.loc[missing, "currency"] = parsed.map(lambda x: x[0])
            still_blank_usd = missing & _blank_mask(out["price_usd"])
            out.loc[still_blank_usd, "price_usd"] = parsed.loc[still_blank_usd].map(lambda x: x[1])
    for col in LISTING_COLUMNS:
        if col not in out.columns:
            out[col] = pd.NA
    out = out[LISTING_COLUMNS].replace("", pd.NA)
    out["currency"] = out["currency"].replace({"US": "USD"})
    for col in ("price", "price_usd"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def listings_csv_needs_repair(path):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return False
    with open(path, newline="", encoding="utf-8") as f:
        rows = [row for row in csv.reader(f) if row]
    if not rows:
        return False
    header, widths = rows[0], {len(r) for r in rows}
    return header != LISTING_COLUMNS or len(widths) > 1


def repair_listings_csv(path=OUT_LISTINGS):
    """Rewrite path so every row shares LISTING_COLUMNS. No-op if already aligned."""
    if not listings_csv_needs_repair(path):
        return False
    aligned = _align_listings(_read_listings_csv(path))
    aligned.to_csv(path, index=False)
    return True


def append_rows(rows, path):
    if not rows:
        return
    new_df = _align_listings(pd.DataFrame(rows))
    if new_df.empty:
        return
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        new_df.to_csv(path, index=False)
        return

    existing = _read_listings_csv(path)
    existing_cols = list(existing.columns)
    ragged = False
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            widths = {len(row) for row in csv.reader(f) if row}
        ragged = len(widths) > 1

    if ragged or existing_cols != LISTING_COLUMNS:
        combined = pd.concat([_align_listings(existing), new_df], ignore_index=True)
        combined.to_csv(path, index=False)
        return

    new_df.to_csv(path, mode="a", header=False, index=False)


def already_done(row):
    """Skip a metal if we already scraped some rows for that brand+metal."""
    if not os.path.exists(OUT_LISTINGS):
        return False
    try:
        existing = _read_listings_csv(OUT_LISTINGS)
        if existing.empty or "brand" not in existing.columns or "metal" not in existing.columns:
            return False
        m = clean_metal(row["metal"])
        mask = (existing["brand"].astype(str) == row["brand"]) & (existing["metal"].astype(str) == m)
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
    """
    Parses eBay's SRP redesign, which uses "s-card" item cards (this replaced
    the older "s-item" markup the scraper originally targeted — confirmed via
    probe_selectors.py against a live page).

    Card structure (from a live probe):
      <li class="s-card ...">
        <div class="su-card-container__media"> ... <a class="s-card__link image-treatment" href="..."> (thumbnail link)
        <div class="su-card-container__content">
          <div class="su-card-container__header">
            <a class="s-card__link" href="...">  (title link, same href as above)
            <div class="s-card__title">...</div>
          <div class="su-card-container__attributes">
            <div class="s-card__attribute-row"><span class="s-card__price">$X</span></div>
            <div class="s-card__attribute-row">...possibly condition text...</div>

    Known gotcha: eBay inserts a decoy/filler card (title literally "Shop on
    eBay", fake price, image alt "Shop on eBay") pointing to the same fake
    itm/123456 honeypot link on every results page. The pre-existing
    "Shop on eBay" title check below was already there for the old .s-item
    markup for exactly this reason — it still applies under s-card.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    cards = soup.select("li.s-card") or soup.select(".s-card")

    CONDITION_KEYWORDS = ("pre-owned", "brand new", "new with", "new without",
                          "refurbished", "open box", "for parts", "used", "certified")

    for card in cards:
        title_el = card.select_one(".s-card__title")
        title = title_el.get_text(" ", strip=True) if title_el else None
        if title:
            # eBay embeds a visually-hidden a11y hint inside the title link's
            # text content ("Opens in a new window or tab") — strip it off.
            title = re.sub(r"\s*Opens in a new window or tab\.?\s*$", "", title, flags=re.IGNORECASE).strip()
        if not title or "shop on ebay" in title.lower():
            continue  # decoy/filler card — see docstring

        link_el = (card.select_one(".su-card-container__header a.s-card__link")
                   or card.select_one("a.s-card__link"))
        href = link_el.get("href") if link_el else None
        item_id_match = re.search(r"/itm/(\d+)", href) if href else None
        item_id = item_id_match.group(1) if item_id_match else None
        if not item_id or len(item_id) < 9:
            continue  # no usable link, or matches the decoy's short fake ID
        url = f"{BASE}/itm/{item_id}"

        price_el = card.select_one(".s-card__price")
        price_text = price_el.get_text(strip=True) if price_el else None
        price = clean_price(price_text) if price_text else None
        currency_code, price_usd = parse_currency(price_text) if price_text else (None, None)

        condition = None
        subtitle_el = card.select_one(".s-card__subtitle-row")
        if subtitle_el:
            # Observed format: "Pre-Owned · Cartier" — condition comes first,
            # separated from brand (and possibly other info) by a middot (·).
            subtitle_text = subtitle_el.get_text(" ", strip=True)
            first_segment = subtitle_text.split("·")[0].strip()
            condition = first_segment or None
        if not condition:
            for row_el in card.select(".s-card__attribute-row"):
                txt = row_el.get_text(" ", strip=True)
                if any(k in txt.lower() for k in CONDITION_KEYWORDS):
                    condition = txt
                    break

        rows.append({
            "title": title,
            "price": price,
            "price_text": price_text,
            "currency": currency_code,
            "price_usd": price_usd,
            "condition": condition,
            "url": url,
        })

    next_link = (soup.select_one("a[aria-label='Next page']") or
                 soup.select_one("a.pagination__next") or
                 soup.select_one("a[aria-label='Next']") or
                 soup.select_one("a[type='next']"))
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
    count_cards = len(soup.select("li.s-card") or soup.select(".s-card"))
    has_results = bool(soup.select_one("#srp-river-results"))
    print(f"[DEBUG {label}] title={title!r} .s-card={count_cards} has_results={has_results}")
    print(html[:400])

# ---------- step 3B: fallback via /itm/ links ----------
def extract_itm_links(html: str, max_links: int = 24):
    """
    Pull real /itm/ links out of a page, filtering out decoy/boilerplate links.

    Note: eBay's markup includes at least one recurring fake link with item ID
    "123456" (seen with itmmeta=012DEW30YG0MEEKND7NH, hash=item123546:...) that
    shows up identically across completely unrelated pages — almost certainly
    template filler or a scraper honeypot, not a real listing. Real eBay item
    IDs are consistently long numeric strings (commonly 12 digits), so we
    filter out anything implausibly short.
    """
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    for a in soup.select('a[href*="/itm/"]'):
        href = a.get("href")
        if not href:
            continue
        href = force_https(absolute(href))
        m = re.search(r"/itm/(\d+)", href)
        if not m:
            continue
        item_id = m.group(1)
        if len(item_id) < 9:  # filters out decoys like "123456"
            continue
        # normalize away tracking query params so we don't treat the same
        # item as "different" across pages, and so retries hit a clean URL
        clean_url = f"{BASE}/itm/{item_id}"
        links.add(clean_url)
        if len(links) >= max_links:
            break
    return list(links)

def parse_item_page(html: str):
    soup = BeautifulSoup(html, "html.parser")

    # --- title ---
    # Broadened: eBay has shipped several template generations, and listing
    # pages don't all use the same markup. Try the known selectors first, then
    # fall back to the page's <title> tag (strip the " | eBay" suffix) rather
    # than giving up and returning None.
    title_el = (soup.select_one("h1.x-item-title__mainTitle")
                or soup.select_one("h1.it-ttl")
                or soup.select_one("[data-testid='x-item-title'] h1")
                or soup.select_one("h1[itemprop='name']")
                or soup.select_one("#itemTitle"))
    title = title_el.get_text(" ", strip=True) if title_el else None
    if title and "Details about" in title:
        title = title.replace("Details about", "").strip(" \xa0")
    if not title and soup.title:
        raw = soup.title.get_text(strip=True)
        title = re.sub(r"\s*\|\s*eBay\s*$", "", raw).strip() or None

    # --- price ---
    price_el = (soup.select_one(".x-price-primary span.ux-textspans")
                or soup.select_one(".x-price-primary")
                or soup.select_one("[data-testid='x-price-primary']")
                or soup.select_one("#prcIsum")
                or soup.select_one("#mm-saleDscPrc")
                or soup.find("span", {"itemprop": "price"})
                or soup.select_one("meta[itemprop='price']"))
    from_meta = bool(price_el and price_el.name == "meta")
    if from_meta:
        price_text = price_el.get("content")
    else:
        price_text = price_el.get_text(strip=True) if price_el else None
    price = clean_price(price_text) if price_text else None

    if from_meta:
        # <meta itemprop="price"> holds a bare number with no currency prefix;
        # eBay pairs it with a separate <meta itemprop="priceCurrency">.
        curr_meta = soup.select_one("meta[itemprop='priceCurrency']")
        currency_code = curr_meta.get("content") if curr_meta else "USD"
        price_usd = round(price * FX_TO_USD.get(currency_code, 1.0), 2) if price is not None else None
    else:
        currency_code, price_usd = parse_currency(price_text) if price_text else (None, None)

    # --- condition ---
    cond_el = (soup.select_one("#vi-itm-cond")
               or soup.select_one(".x-item-condition-text")
               or soup.select_one("[data-testid='x-item-condition'] .ux-textspans")
               or soup.select_one(".ux-icon-text__accessibleTxt"))
    condition = cond_el.get_text(" ", strip=True) if cond_el else None

    # --- item specifics table (metal, weight, purity, length, stone, etc.) ---
    # NOTE: eBay has used at least two different layouts for this section over
    # time — (a) legacy "Label: Value" text in one table row/list item, and
    # (b) modern paired label/value elements (e.g. dt/dd, or sibling divs whose
    # classes contain "labels-content" / "values-content"). We handle both.
    # This section is the most likely to need re-tuning if eBay changes markup
    # again — verify against a live item page if specifics come back empty.
    specifics = {}

    # (a) legacy colon-separated rows
    for row in soup.select(
        "#vi-desc-maincntr table tr, "
        ".x-about-this-item table tr, "
        ".x-about-this-item li"
    ):
        txt = row.get_text(" ", strip=True)
        if ":" in txt:
            k, v = txt.split(":", 1)
            specifics.setdefault(k.strip(), v.strip())

    # (b) modern paired label/value elements
    label_els = soup.select("dt, [class*='labels-content']")
    value_els = soup.select("dd, [class*='values-content']")
    for k_el, v_el in zip(label_els, value_els):
        k = k_el.get_text(" ", strip=True)
        v = v_el.get_text(" ", strip=True)
        if k and v:
            specifics.setdefault(k, v)

    def first_match(*keys):
        for key in keys:
            for spec_key, spec_val in specifics.items():
                if spec_key.strip().lower() == key.lower():
                    return spec_val
        return None

    metal_val = first_match("Metal", "Base Metal")
    purity_val = first_match("Metal Purity", "Purity", "Fineness")
    weight_val = first_match("Total Carat Weight", "Weight", "Item Weight")
    length_val = first_match("Length", "Chain Length", "Necklace Length")

    return {
        "title": title,
        "price": price,
        "price_text": price_text,
        "currency": currency_code,
        "price_usd": price_usd,
        "condition": condition,
        "metal_item_page": metal_val,
        "purity_item_page": purity_val,
        "weight_item_page": weight_val,
        "length_item_page": length_val,
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
def run(max_brands=None, max_metals_per_brand=None, max_pages=3, max_items_fallback=40, force=False):
    """
    max_brands:            cap on how many brands to process (None = all found)
    max_metals_per_brand:  cap on metal facets per brand (None = all found)
    max_pages:             SRP pages to paginate through per brand+metal facet
    max_items_fallback:    /itm/ links to pull when the SRP-scrape fallback triggers
    force:                 re-scrape brand+metal combos even if already checkpointed
    """
    if repair_listings_csv(OUT_LISTINGS):
        print(f"Aligned listings CSV to current columns → {OUT_LISTINGS}")

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

    if max_brands is not None:
        brands = brands[:max_brands]
        print(f"Limiting to first {max_brands} brands this run (--max-brands)")

    # 2) Metals per brand
    metal_rows = []
    for i, b in enumerate(brands, 1):
        try:
            print(f"[{i}/{len(brands)}] {b['brand']} -> {b['brand_url']}")
            snooze(3.0, 2.5)
            brand_html = fetch(b["brand_url"], referer=seed_marketing)
            metals = parse_shop_by_metal(brand_html, b["brand"], b["brand_url"])

            if max_metals_per_brand is not None:
                metals = metals[:max_metals_per_brand]
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
    summary = {"brands_metals_attempted": 0, "brands_metals_with_data": 0, "total_listings": 0}
    for j, row in enumerate(metal_rows, 1):
        try:
            metal_clean = clean_metal(row["metal"])

            if not force and already_done(row):
                print(f"[{j}/{len(metal_rows)}] Skip {row['brand']} – {metal_clean} (already scraped; use --force to re-scrape)")
                continue

            summary["brands_metals_attempted"] += 1
            print(f"[{j}/{len(metal_rows)}] Listings: {row['brand']} – {metal_clean}")
            snooze(4.0, 3.0)

            # Try the keyword-search SRP first: testing shows the "Shop by
            # Metal" facet URL (row['metal_url']) often renders as a JS shell
            # with no items in the static HTML at all (has_results=False),
            # while the keyword-search SRP is server-rendered and does return
            # a populated results container (has_results=True) — it just may
            # need its item-card selector kept in sync with eBay's markup.
            fallback = srp_url_for(row["brand"], metal_clean)
            listings = paginate_listings(fallback, max_pages=max_pages, referer=row["brand_url"])

            # Try the facet URL as a secondary attempt (cheap to try, occasionally works)
            if not listings:
                print("  → Keyword SRP empty; trying metal facet URL:", row["metal_url"])
                listings = paginate_listings(row["metal_url"], max_pages=max_pages, referer=row["brand_url"])

            # Final fallback: /itm/ extraction + item-page parsing
            if not listings:
                print("  → Facet URL also empty; extracting /itm/ links and parsing item pages…")
                listings = scrape_items_via_itm_links(fallback, max_items=max_items_fallback, referer=row["brand_url"])

            # Attach context + append immediately (checkpoint)
            for r in listings:
                r.update({"brand": row["brand"], "metal": metal_clean, "metal_url": row["metal_url"]})
            append_rows(listings, OUT_LISTINGS)

            usable = sum(1 for r in listings if r.get("title") and r.get("price") is not None)
            if usable:
                summary["brands_metals_with_data"] += 1
            summary["total_listings"] += usable

            print(f"  → appended {len(listings)} rows ({usable} with usable title+price) to {OUT_LISTINGS}")
            snooze(5.0, 3.5)  # longer pause between metals
        except Exception as e:
            print(f"  ! Skipped listings for {row['brand']} – {row['metal']}: {e}")

    print("\n----- RUN SUMMARY -----")
    print(f"Brand/metal combos attempted:      {summary['brands_metals_attempted']}")
    print(f"Combos that yielded usable rows:    {summary['brands_metals_with_data']}")
    print(f"Total usable listings this run:     {summary['total_listings']}")
    print("------------------------\n")

def parse_args():
    p = argparse.ArgumentParser(description="Scrape eBay Fine Jewelry listings by brand/metal.")
    p.add_argument("--max-brands", type=int, default=None,
                    help="Cap on number of brands to process (default: all found)")
    p.add_argument("--max-metals-per-brand", type=int, default=None,
                    help="Cap on metal facets per brand (default: all found)")
    p.add_argument("--max-pages", type=int, default=3,
                    help="SRP pages to paginate through per brand+metal (default: 3)")
    p.add_argument("--max-items-fallback", type=int, default=40,
                    help="Items to pull via /itm/ fallback per brand+metal (default: 40)")
    p.add_argument("--skip-sanity-check", action="store_true",
                    help="Skip the single-item sanity check before the full run")
    p.add_argument("--force", action="store_true",
                    help="Re-scrape brand+metal combos even if already checkpointed in the CSV")
    p.add_argument("--repair-csv", action="store_true",
                    help="Rewrite the listings CSV to the current column layout and exit")
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()

    if args.repair_csv:
        if repair_listings_csv(OUT_LISTINGS):
            print(f"Rewrote {OUT_LISTINGS} to a single column layout")
        else:
            print(f"Nothing to repair ({OUT_LISTINGS} missing or empty)")
        raise SystemExit(0)

    if not args.skip_sanity_check:
        # Quick sanity test for a single item page — confirms selectors still
        # match eBay's current markup before burning time on a full run.
        try:
            test_item = "https://www.ebay.com/itm/286830144288"
            html = fetch(test_item, referer="https://www.ebay.com/")
            print("[sanity item]", parse_item_page(html))
        except Exception as e:
            print("[sanity item] skipped:", e)

    # Full pipeline:
    run(
        max_brands=args.max_brands,
        max_metals_per_brand=args.max_metals_per_brand,
        max_pages=args.max_pages,
        max_items_fallback=args.max_items_fallback,
        force=args.force,
    )
