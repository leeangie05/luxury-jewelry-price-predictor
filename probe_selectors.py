# probe_selectors.py
#
# To run locally against a live eBay search URL to figure out what
# CSS class eBay is currently using for its item cards. The scraper's
# `.s-item` selector is returning 0 matches even on pages that have results,
# so eBay probably changed the class name.
#
# This looks through the page HTML for classes related to items, listings,
# cards, etc. and shows how many times each class appears. A real item-card
# class should repeat roughly once per listing
#
# Also prints examples of the HTML for the card, title, price, and link
# so we can figure out which selectors to use in `parse_listings_page()`.
#
# Usage:
#   python probe_selectors.py "https://www.ebay.com/sch/i.html?_sacat=4196&_nkw=David+Yurman+%22Silver%22&rt=nc&_ipg=120"

import sys
from collections import Counter
from scraper import fetch, HEADERS  # reuse the same session/headers/anti-bot handling


def probe(url: str):
    html = fetch(url)
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")

    counter = Counter()
    for tag in soup.find_all(True, class_=True):
        for cls in tag.get("class", []):
            low = cls.lower()
            if any(k in low for k in ("item", "card", "result", "listing", "tile")):
                counter[cls] += 1

    print(f"\nTop candidate classes on {url}\n" + "-" * 60)
    for cls, count in counter.most_common(25):
        print(f"{count:5d}  {cls}")

    # Show the actual repeating card container
    # The class whose count is closest to a "reasonable number of listings per page"
    # NOT the most common class overall, which is often a sub-element that repeats multiple times per card
    print("\n" + "=" * 60)
    for target in ["s-card", "s-item", "su-card-container"]:
        el = soup.find(class_=target)
        if el:
            print(f"\nFull outerHTML of first element with class '{target}':\n")
            print(str(el)[:3000])
            print("\n" + "-" * 60)

    # Specifically show where title/price/link live relative to the card
    for target in ["s-card__title", "s-card__price", "s-card__link"]:
        el = soup.find(class_=target)
        if el:
            print(f"\nExample '{target}' element (tag={el.name}, attrs={el.attrs}):")
            print(str(el)[:500])
            print("-" * 60)


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else (
        "https://www.ebay.com/sch/i.html?_sacat=4196&_nkw=David+Yurman+%22Silver%22&rt=nc&_ipg=120"
    )
    probe(url)
