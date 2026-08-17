# Luxury Jewelry Price Predictor

End-to-end data project: scrape eBay Fine Jewelry listings, clean messy marketplace text, and model asking price from brand, metal, and title-derived features.

**1,309 unique USD listings** across Tiffany & Co., David Yurman, and Cartier. Asking prices, not sold prices.

| | |
|---|---|
| Median asking price | $992 |
| Mean (skewed by Cartier tail) | $2,849 |
| Best model | Random forest, MAE **$988**, R² **0.38** |
| Test-set median | $1,020 |

The model can rank a Cartier gold ring above a Tiffany silver charm. It cannot appraise a specific piece. That gap is the finding, not a failure of the pipeline.

## What I built

1. **Scraper** (`scraper.py`) — paginates eBay search results by brand and metal, with retries, anti-bot backoff, checkpointing so a run can resume, and fallbacks when a results page is a JS shell.
2. **Cleaning + features** — drop failed fetches and duplicate item IDs (same listing, different tracking URLs), keep USD, pull item type / karat / weight out of titles.
3. **EDA + baselines** (`notebooks/jewelry_price_predictor.ipynb`) — price by brand, metal, and type; linear regression vs random forest on a train/test split.

## Findings

- **Brand is the strongest split.** Cartier median $2,895 vs Tiffany $594 vs David Yurman $349. Cartier’s mean ($6,843) is pulled up by a few six-figure Panthère listings.
- **Metal is the other real axis.** Sterling silver sits around $250 for Tiffany and Yurman. Yellow gold is ~$1,250 for those two and $3,350 for Cartier.
- **Condition barely exists in this scrape.** 1,174 / 1,309 rows are just “Pre-Owned” because search pages do not expose Good/Excellent. Weight appears in 62 titles. Item-page specifics (grams, stones, purity) are empty for every row.
- **So the ceiling is categorical ranking**, not a dollar-accurate predictor, until the scraper hits individual item pages.

## Technical notes

The interesting work was getting eBay HTML to parse at all:

- Search-result selectors changed (`s-item` → `s-card`); the scraper tries current card markup, then `/itm/` link extraction.
- Bot interstitial pages are detected and backed off instead of written out as empty rows.
- Listings are appended per brand/metal so a long run can stop and continue.
- Title parsing has to check “earrings” before “ring” (`ring` is a substring of `earrings`).

Stack: Python, requests, BeautifulSoup, pandas, NumPy, scikit-learn, matplotlib, seaborn, Jupyter.

## Run it

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt requests beautifulsoup4
```

**Notebook** (uses the CSVs already in `data/`):

```bash
jupyter notebook notebooks/jewelry_price_predictor.ipynb
```

**Scraper** (hits live eBay; start small):

```bash
python scraper.py --max-brands 1 --max-metals-per-brand 1 --max-pages 1
```

Use `--force` to re-scrape a brand/metal already in the CSV. A full run is slow on purpose (polite delays). Outputs:

- `data/ebay_fine_jewelry_listings_sample.csv` — raw scrape
- `data/ebay_fine_jewelry_cleaned.csv` — written by the notebook

## Project layout

```
├── scraper.py              # eBay Fine Jewelry scraper
├── notebooks/
│   └── jewelry_price_predictor.ipynb
├── data/
│   ├── ebay_fine_jewelry_listings_sample.csv
│   ├── ebay_fine_jewelry_cleaned.csv
│   └── ebay_fine_jewelry_metals.csv
├── probe_selectors.py      # debug eBay card CSS
├── quick_test.py           # one-page parse check
└── requirements.txt
```

## If I kept going

Pull item pages so weight, purity, and stones are real columns. Add Chanel and Van Cleef (they already show up on the Fine Jewelry brands page). Filter auction-start prices ($0.99). Try gradient boosting once those features exist. Create an interactive web app to predict users jewelry price if sold online.
