# quick_test.py — bypasses brand/metal orchestration and checkpointing entirely
# Directly tests parse_listings_page() against a live SRP fetch
import scraper as s
import json
from bs4 import BeautifulSoup

url = s.srp_url_for("Cartier", "Yellow Gold")
print("Fetching:", url)
html = s.fetch(url)
rows, next_url = s.parse_listings_page(html)

print(f"\nParsed {len(rows)} listings.\n")
for r in rows[:5]:
    print(json.dumps(r, indent=2))
print("...\nnext_url:", next_url)

# (debug) dump raw attribute-row text for the first few real cards
# to see what's actually in there since condition isn't matching
print("\n" + "=" * 60)
print("DEBUG: raw .s-card__attribute-row text per card (first 4 real cards)")
print("=" * 60)
soup = BeautifulSoup(html, "html.parser")
cards = soup.select("li.s-card") or soup.select(".s-card")
shown = 0
for card in cards:
    title_el = card.select_one(".s-card__title")
    title = title_el.get_text(" ", strip=True) if title_el else None
    if not title or "shop on ebay" in title.lower():
        continue
    rows_text = [r.get_text(" ", strip=True) for r in card.select(".s-card__attribute-row")]
    subtitle_text = [r.get_text(" ", strip=True) for r in card.select(".s-card__subtitle-row")]
    print(f"\nTitle: {title[:60]}")
    print(f"  attribute-rows:  {rows_text}")
    print(f"  subtitle-rows:   {subtitle_text}")
    shown += 1
    if shown >= 4:
        break
