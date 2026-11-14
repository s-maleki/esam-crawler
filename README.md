# esam.ir listing crawler

Export public seller listings and search results from [esam.ir](https://esam.ir) to CSV.

The site is a Next.js app. Listing cards are loaded as public JSON from `api.esam.ir`, which is what this tool reads:

- Seller listings: `GET https://api.esam.ir/api/items/SellerAllItems?user=<username>&page=<n>&pageSize=<n>`
- Search: `GET https://api.esam.ir/api/items/query?page=<n>&pageSize=<n>&searchPhrase=<query>`

Those two paths are allowed by [api.esam.ir/robots.txt](https://api.esam.ir/robots.txt). [esam.ir/robots.txt](https://esam.ir/robots.txt) disallows HTML search URLs that contain `kw=` (`Disallow: /search*kw=` and `Disallow: /Search*kw=`), so this tool never requests them. It does not log in, does not follow redirects, and does not call tracking endpoints.

The seller page that was checked live is:

https://esam.ir/aboutSeller?activeTab=items&orderusername=kolokolchik

The public seller id in that URL is `orderusername` (a username). The numeric uid on that seller (`4961028`) is not accepted by `SellerAllItems` and returns no rows.

Listing cards do not include a posted date. `remain_seconds` is the time field the API returns (seconds remaining on the listing). Prices are the integers returned by the API. The site's own product markup labels those amounts as IRR.

## Install

Python 3.10 or newer.

```bash
cd esam-crawler
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Dependencies are `requests` and `psycopg` (v3). Pages expose listings as JSON, so the crawler does not parse HTML and does not use a browser. CSV output does not need a database. Postgres output needs a URL you supply at run time.

## Commands

All of a seller's listings:

```bash
python crawler.py seller \
  --url "https://esam.ir/aboutSeller?activeTab=items&orderusername=kolokolchik" \
  --out seller.csv
```

The same seller by username:

```bash
python crawler.py seller --seller kolokolchik --out seller.csv
```

Search:

```bash
python crawler.py search --query "کتاب" --out search.csv
```

Useful options, on either command:

- `--limit N` stops after N listings. Omit it to keep paging until the list ends.
- `--delay SECONDS` waits between requests. The default is 1 and values below 1 are raised to 1.
- `--page-size N` is how many listings to ask for per request (1–200, default 50). The website itself asks for 200.
- `--out PATH` is the CSV file, written as UTF-8 with a BOM so spreadsheets open Persian text correctly.
- `--dsn URL` also upserts those rows into Postgres. If you omit it and `DATABASE_URL` is set, that variable is used instead. The crawler never prints the URL. With neither one set, it writes only the CSV.

The client sends `User-Agent: EsamListingCrawler/1.0 (public listing export; respects robots.txt; +https://esam.ir)`, reads each host's robots.txt before requesting a URL, and stops after 3 failed attempts on the same URL.

Paging stops when a page is empty, shorter than the page size, repeats items already saved, or the API `total_count` is reached.

## Columns

Both modes write the same columns. `query` is the search phrase; seller rows leave it blank.

`query`, `item_id`, `title`, `price`, `old_price`, `discount_percent`, `url`, `condition`, `condition_code`, `location`, `seller_username`, `seller_id`, `seller_url`, `image_url`, `image_thumb_url`, `is_auction`, `sale_type`, `bid_count`, `category_code`, `free_shipping`, `shipping_payment`, `is_fake`, `is_first_sell`, `is_special_discount`, `is_immediate_service`, `is_advertisement`, `is_asansor`, `is_khanetekani`, `seller_feedback_percent`, `seller_feedback_score`, `remain_seconds`, `main_image`

`seller_url` is rebuilt as `/aboutSeller?activeTab=items&orderusername=<username>`, which is the seller page shape above. `url` is the absolute item link. Empty cells are fields the API returned as null. There is no separate date column because the listing payload has no posted date.

## Sample files

These files were written from the live site with `--limit 5`.

```bash
python crawler.py seller \
  --url "https://esam.ir/aboutSeller?activeTab=items&orderusername=kolokolchik" \
  --out seller-sample.csv \
  --limit 5
```

`seller-sample.csv` has 5 rows. The API reported `total_count` 2568 for that seller. A separate run with `--page-size 2 --limit 4` requested two pages and saved four different item ids.

```bash
python crawler.py search --query "کتاب" --out search-sample.csv --limit 5
```

`search-sample.csv` has 5 rows. The API reported `total_count` 90000 for `کتاب`. Search rows in that sample have an empty `location` because the payload's `city` was null. The seller sample's location is `تهران`.

Neither payload includes a posted date, so `remain_seconds` is the only time column and it is filled from the API value.

## Postgres

Seller and search both write the same table when a database URL is present. `load-csv` loads a file this crawler already wrote, without crawling again.

Table `esam_listings`:

- One text column for each CSV column, in the same order. `item_id` is the primary key.
- `fetched_at timestamptz` is set when the row is written and replaced on every upsert.

A later crawl of the same listing updates that row (`ON CONFLICT (item_id) DO UPDATE`). It does not insert a second copy. Seller rows and search rows share the table, so a search that returns an item already stored from a seller crawl updates the same `item_id` and replaces `query` with the search phrase.

```bash
export DATABASE_URL="postgresql://USER:PASSWORD@HOST:5432/DBNAME"
python crawler.py seller --seller kolokolchik --out seller.csv
```

Or pass the URL only for that command:

```bash
python crawler.py search --query "کتاب" --out search.csv --dsn "$DATABASE_URL"
```

Load an existing CSV:

```bash
python crawler.py load-csv --csv seller.csv --dsn "$DATABASE_URL"
```

`--limit` on `load-csv` reads only the first N data rows.

`abaan.csv` in this folder is a local copy of that seller's current listings. This project does not load it, or any other CSV, into a database until you pass `--dsn` or set `DATABASE_URL`. There is no database URL in the repo.
