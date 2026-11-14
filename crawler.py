#!/usr/bin/env python3
"""Export public esam.ir seller listings and search results to CSV.

When --dsn or DATABASE_URL is set, the same rows are also upserted into
Postgres. The connection URL is never printed.

Listings are the JSON documents the public site already loads:

- seller pages call GET https://api.esam.ir/api/items/SellerAllItems
- search calls GET https://api.esam.ir/api/items/query

Those paths are allowed by https://api.esam.ir/robots.txt. The HTML search
URLs that contain ``kw=`` are disallowed by https://esam.ir/robots.txt, so
this tool never requests them.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Iterator
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests

USER_AGENT = (
    "EsamListingCrawler/1.0 "
    "(public listing export; respects robots.txt; +https://esam.ir)"
)
SITE_ORIGIN = "https://esam.ir"
API_ORIGIN = "https://api.esam.ir"
SELLER_API_PATH = "/api/items/SellerAllItems"
SEARCH_API_PATH = "/api/items/query"
DEFAULT_DELAY_SECONDS = 1.0
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT = 30
LISTING_TABLE = "esam_listings"
UPSERT_BATCH = 500

COLUMNS = [
    "query",
    "item_id",
    "title",
    "price",
    "old_price",
    "discount_percent",
    "url",
    "condition",
    "condition_code",
    "location",
    "seller_username",
    "seller_id",
    "seller_url",
    "image_url",
    "image_thumb_url",
    "is_auction",
    "sale_type",
    "bid_count",
    "category_code",
    "free_shipping",
    "shipping_payment",
    "is_fake",
    "is_first_sell",
    "is_special_discount",
    "is_immediate_service",
    "is_advertisement",
    "is_asansor",
    "is_khanetekani",
    "seller_feedback_percent",
    "seller_feedback_score",
    "remain_seconds",
    "main_image",
]


class CrawlError(RuntimeError):
    """The crawl stopped because requests kept failing or a response was unusable."""


@dataclass(frozen=True)
class RobotRule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]


@dataclass
class RobotFile:
    rules: list[RobotRule]
    crawl_delay: float | None = None

    def allows(self, path_and_query: str) -> bool:
        matched: list[RobotRule] = [
            rule for rule in self.rules if rule.regex.match(path_and_query)
        ]
        if not matched:
            return True
        longest = max(len(rule.pattern) for rule in matched)
        winners = [rule for rule in matched if len(rule.pattern) == longest]
        # Same-length Allow beats Disallow, matching Google's robots.txt behavior.
        return any(rule.allow for rule in winners)


def _compile_robot_pattern(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    pieces: list[str] = []
    for char in body:
        pieces.append(".*" if char == "*" else re.escape(char))
    expression = "".join(pieces)
    if anchored:
        expression += "$"
    return re.compile(expression)


def parse_robots(text: str, user_agent: str = USER_AGENT) -> RobotFile:
    """Parse robots.txt groups, including the ``*`` and ``$`` this site uses."""
    groups: list[tuple[list[str], list[tuple[str, str]], float | None]] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    crawl_delay: float | None = None
    seen_directive = False

    def flush() -> None:
        nonlocal agents, rules, crawl_delay, seen_directive
        if agents or rules:
            groups.append((agents, rules, crawl_delay))
        agents, rules, crawl_delay = [], [], None
        seen_directive = False

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            if seen_directive:
                flush()
            agents.append(value.lower())
        elif key in {"allow", "disallow"}:
            seen_directive = True
            rules.append((key, value))
        elif key == "crawl-delay":
            seen_directive = True
            try:
                crawl_delay = float(value)
            except ValueError:
                pass
    flush()

    ua = user_agent.lower()
    chosen: list[tuple[list[str], list[tuple[str, str]], float | None]] = []
    star: list[tuple[list[str], list[tuple[str, str]], float | None]] = []
    for group in groups:
        group_agents = group[0]
        if any(agent != "*" and agent in ua for agent in group_agents):
            chosen.append(group)
        elif "*" in group_agents:
            star.append(group)
    selected = chosen or star

    compiled: list[RobotRule] = []
    delay: float | None = None
    for _, group_rules, group_delay in selected:
        if group_delay is not None:
            delay = group_delay if delay is None else max(delay, group_delay)
        for kind, pattern in group_rules:
            if pattern == "":
                continue
            compiled.append(
                RobotRule(
                    allow=kind == "allow",
                    pattern=pattern,
                    regex=_compile_robot_pattern(pattern),
                )
            )
    return RobotFile(compiled, delay)


def path_and_query(url: str) -> str:
    parsed = urlparse(url)
    if parsed.query:
        return f"{parsed.path}?{parsed.query}"
    return parsed.path or "/"


class Client:
    def __init__(self, delay: float) -> None:
        if delay < DEFAULT_DELAY_SECONDS:
            print(
                f"Raising delay from {delay} to {DEFAULT_DELAY_SECONDS} seconds.",
                file=sys.stderr,
            )
            delay = DEFAULT_DELAY_SECONDS
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain;q=0.9, */*;q=0.8",
            }
        )
        self._robots: dict[str, RobotFile] = {}
        self._last_request_at = 0.0

    def _wait(self) -> None:
        if not self._last_request_at:
            return
        remaining = self.delay - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    def _mark(self) -> None:
        self._last_request_at = time.monotonic()

    def robots_for(self, origin: str) -> RobotFile:
        origin = origin.rstrip("/")
        cached = self._robots.get(origin)
        if cached is not None:
            return cached
        url = f"{origin}/robots.txt"
        text = self._get_text(url, check_robots=False)
        parsed = parse_robots(text)
        if parsed.crawl_delay and parsed.crawl_delay > self.delay:
            print(
                f"{origin} crawl-delay is {parsed.crawl_delay}s; using that.",
                file=sys.stderr,
            )
            self.delay = parsed.crawl_delay
        self._robots[origin] = parsed
        return parsed

    def ensure_allowed(self, url: str) -> None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        rules = self.robots_for(origin)
        target = path_and_query(url)
        if not rules.allows(target):
            raise CrawlError(f"robots.txt disallows {url}")

    def _get_text(self, url: str, check_robots: bool) -> str:
        if check_robots:
            self.ensure_allowed(url)
        last_error = "no response"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._wait()
            try:
                response = self.session.get(
                    url, timeout=REQUEST_TIMEOUT, allow_redirects=False
                )
            except requests.RequestException as exc:
                self._mark()
                last_error = str(exc)
                print(
                    f"Request failed ({attempt}/{MAX_ATTEMPTS}) {url}: {last_error}",
                    file=sys.stderr,
                )
                continue
            self._mark()
            if response.is_redirect or response.is_permanent_redirect:
                last_error = (
                    f"HTTP {response.status_code} redirect to "
                    f"{response.headers.get('Location', '')}"
                )
                print(
                    f"Request failed ({attempt}/{MAX_ATTEMPTS}) {url}: {last_error}",
                    file=sys.stderr,
                )
                continue
            if response.status_code >= 400:
                last_error = f"HTTP {response.status_code}"
                print(
                    f"Request failed ({attempt}/{MAX_ATTEMPTS}) {url}: {last_error}",
                    file=sys.stderr,
                )
                continue
            response.encoding = response.encoding or "utf-8"
            return response.text
        raise CrawlError(f"Stopped after {MAX_ATTEMPTS} errors for {url}: {last_error}")

    def get_json(self, url: str, params: dict[str, object], referer: str) -> dict:
        prepared = requests.Request("GET", url, params=params).prepare()
        final_url = prepared.url or url
        self.ensure_allowed(final_url)
        last_error = "no response"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._wait()
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers={"Referer": referer},
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                self._mark()
                last_error = str(exc)
                print(
                    f"Request failed ({attempt}/{MAX_ATTEMPTS}) {final_url}: {last_error}",
                    file=sys.stderr,
                )
                continue
            self._mark()
            if response.is_redirect or response.is_permanent_redirect:
                last_error = (
                    f"HTTP {response.status_code} redirect to "
                    f"{response.headers.get('Location', '')}"
                )
                print(
                    f"Request failed ({attempt}/{MAX_ATTEMPTS}) {final_url}: {last_error}",
                    file=sys.stderr,
                )
                continue
            if response.status_code >= 400:
                last_error = f"HTTP {response.status_code}"
                print(
                    f"Request failed ({attempt}/{MAX_ATTEMPTS}) {final_url}: {last_error}",
                    file=sys.stderr,
                )
                continue
            try:
                payload = response.json()
            except ValueError:
                last_error = "response was not JSON"
                print(
                    f"Request failed ({attempt}/{MAX_ATTEMPTS}) {final_url}: {last_error}",
                    file=sys.stderr,
                )
                continue
            if not isinstance(payload, dict):
                last_error = "JSON root was not an object"
                print(
                    f"Request failed ({attempt}/{MAX_ATTEMPTS}) {final_url}: {last_error}",
                    file=sys.stderr,
                )
                continue
            status = payload.get("statusCode")
            if status not in (0, None):
                last_error = f"statusCode {status}: {payload.get('message') or ''}".strip()
                print(
                    f"Request failed ({attempt}/{MAX_ATTEMPTS}) {final_url}: {last_error}",
                    file=sys.stderr,
                )
                continue
            return payload
        raise CrawlError(
            f"Stopped after {MAX_ATTEMPTS} errors for {final_url}: {last_error}"
        )


def absolute_item_url(link: object) -> str:
    if not isinstance(link, str) or not link.strip():
        return ""
    link = link.strip()
    if link.startswith("http://") or link.startswith("https://"):
        return link
    return urljoin(SITE_ORIGIN + "/", link.lstrip("/"))


def seller_page_url(username: str) -> str:
    if not username:
        return ""
    return (
        SITE_ORIGIN
        + "/aboutSeller?"
        + urlencode({"activeTab": "items", "orderusername": username})
    )


def seller_username_from_url(url: str) -> str:
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path.rstrip("/") or "/"
    if parsed.scheme not in {"http", "https"} or host != "esam.ir" or path != "/aboutSeller":
        raise CrawlError(
            "Seller URL must be an https://esam.ir/aboutSeller page. "
            "Verified shape: "
            "https://esam.ir/aboutSeller?activeTab=items&orderusername=kolokolchik"
        )
    decoded = {key.lower(): values for key, values in parse_qs(parsed.query).items()}
    values = decoded.get("orderusername") or []
    username = values[0].strip() if values else ""
    if not username:
        raise CrawlError(
            "No orderusername in the seller URL. The public seller id on esam.ir "
            "is the username, for example "
            "https://esam.ir/aboutSeller?activeTab=items&orderusername=kolokolchik"
        )
    return username


def cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def row_from_item(item: dict, query: str) -> dict[str, str]:
    username = item.get("seller_username")
    username_text = username.strip() if isinstance(username, str) else ""
    item_id = item.get("idItem")
    if item_id is None:
        item_id = item.get("id")
    return {
        "query": query,
        "item_id": cell(item_id),
        "title": cell(item.get("item_name")),
        "price": cell(item.get("price")),
        "old_price": cell(item.get("old_price")),
        "discount_percent": cell(item.get("discount_percent")),
        "url": absolute_item_url(item.get("item_link")),
        "condition": cell(item.get("item_condition_text")),
        "condition_code": cell(item.get("item_condition")),
        "location": cell(item.get("city")),
        "seller_username": username_text,
        "seller_id": cell(item.get("seller_id")),
        "seller_url": seller_page_url(username_text),
        "image_url": cell(item.get("item_big_thumb_url")),
        "image_thumb_url": cell(item.get("item_small_thumb_url")),
        "is_auction": cell(item.get("is_auction")),
        "sale_type": cell(item.get("auctionOrFixPrice")),
        "bid_count": cell(item.get("auction_bid_count")),
        "category_code": cell(item.get("categoryCode")),
        "free_shipping": cell(item.get("is_free_shipping")),
        "shipping_payment": cell(item.get("post_paymentType_title")),
        "is_fake": cell(item.get("is_fake")),
        "is_first_sell": cell(item.get("isFirstSell")),
        "is_special_discount": cell(item.get("isSpecialDiscount")),
        "is_immediate_service": cell(item.get("isImmediateService")),
        "is_advertisement": cell(item.get("isAdvertisment")),
        "is_asansor": cell(item.get("isAsansor")),
        "is_khanetekani": cell(item.get("isKhanetekani")),
        "seller_feedback_percent": cell(item.get("feedback_positive")),
        "seller_feedback_score": cell(item.get("feedback_score_total")),
        "remain_seconds": cell(item.get("remain_seconds")),
        "main_image": cell(item.get("main_image")),
    }


def iter_pages(
    fetch_page: Callable[[int, int], dict],
    limit: int | None,
    page_size: int,
) -> Iterator[dict]:
    page = 1
    seen: set[object] = set()
    yielded = 0
    total: int | None = None
    while True:
        if limit is not None and yielded >= limit:
            return
        size = page_size
        if limit is not None:
            size = min(size, limit - yielded)
        payload = fetch_page(page, size)
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            raise CrawlError("Listing payload had no data object")
        if total is None and isinstance(data.get("total_count"), int):
            total = data["total_count"]
        items = data.get("items") or []
        if not isinstance(items, list):
            raise CrawlError("Listing payload items was not a list")
        print(
            f"page {page}: {len(items)} items"
            + (f" (total_count {total})" if total is not None else ""),
            file=sys.stderr,
        )
        if not items:
            return
        new_on_page = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("idItem", item.get("id"))
            if item_id is not None and item_id in seen:
                continue
            if item_id is not None:
                seen.add(item_id)
            new_on_page += 1
            yield item
            yielded += 1
            if limit is not None and yielded >= limit:
                return
            if total is not None and total > 0 and yielded >= total:
                return
        if new_on_page == 0:
            print("Stopped because this page repeated items already saved.", file=sys.stderr)
            return
        if len(items) < size:
            return
        page += 1


def write_csv(path: str, rows: Iterable[dict[str, str]]) -> int:
    count = 0
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def database_dsn(explicit: str | None) -> str | None:
    """Return --dsn when it was passed, otherwise DATABASE_URL. Never log it."""
    if explicit is not None:
        dsn = explicit.strip()
        if not dsn:
            raise CrawlError("--dsn was empty")
        return dsn
    env = os.environ.get("DATABASE_URL", "").strip()
    return env or None


def _hide_dsn(message: str, dsn: str) -> str:
    hidden = message.replace(dsn, "[dsn]") if dsn else message
    hidden = re.sub(r"postgres(?:ql)?://\S+", "[dsn]", hidden, flags=re.IGNORECASE)
    hidden = re.sub(
        r"(?i)(password\s*=\s*)('[^']*'|\"[^\"]*\"|\S+)",
        r"\1[redacted]",
        hidden,
    )
    return hidden


def _listing_rows(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    """Keep the last copy of each item_id. Postgres cannot upsert one id twice per statement."""
    order: list[str] = []
    by_id: dict[str, dict[str, str]] = {}
    for raw in rows:
        item_id = str(raw.get("item_id") or "").strip()
        if not item_id:
            raise CrawlError("A listing row is missing item_id")
        row = {column: "" if raw.get(column) is None else str(raw.get(column)) for column in COLUMNS}
        row["item_id"] = item_id
        if item_id not in by_id:
            order.append(item_id)
        by_id[item_id] = row
    return [by_id[item_id] for item_id in order]


def read_csv_rows(path: str, limit: int | None) -> list[dict[str, str]]:
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        missing = [column for column in COLUMNS if column not in fieldnames]
        if missing:
            raise CrawlError("CSV is missing columns: " + ", ".join(missing))
        rows: list[dict[str, str]] = []
        for raw in reader:
            rows.append({column: raw.get(column) or "" for column in COLUMNS})
            if limit is not None and len(rows) >= limit:
                break
    return rows


def upsert_listings(dsn: str, rows: Iterable[dict[str, str]], fetched_at: datetime) -> int:
    """Create esam_listings if needed and upsert on item_id. Does not print dsn."""
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:
        raise CrawlError(
            "Postgres output needs psycopg. Install it with pip install -r requirements.txt"
        ) from exc

    prepared = _listing_rows(rows)
    columns = [*COLUMNS, "fetched_at"]
    updates = [column for column in columns if column != "item_id"]
    create_sql = sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(
        sql.Identifier(LISTING_TABLE),
        sql.SQL(", ").join(
            [
                sql.SQL("{} text PRIMARY KEY").format(sql.Identifier("item_id"))
                if column == "item_id"
                else sql.SQL("{} text NOT NULL DEFAULT ''").format(sql.Identifier(column))
                for column in COLUMNS
            ]
            + [sql.SQL("fetched_at timestamptz NOT NULL")]
        ),
    )
    upsert_sql = sql.SQL(
        "INSERT INTO {table} ({fields}) VALUES ({placeholders}) "
        "ON CONFLICT (item_id) DO UPDATE SET {updates}"
    ).format(
        table=sql.Identifier(LISTING_TABLE),
        fields=sql.SQL(", ").join(sql.Identifier(column) for column in columns),
        placeholders=sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        updates=sql.SQL(", ").join(
            sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(column), sql.Identifier(column))
            for column in updates
        ),
    )
    params = [tuple(row[column] for column in COLUMNS) + (fetched_at,) for row in prepared]
    try:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(create_sql)
                for start in range(0, len(params), UPSERT_BATCH):
                    cur.executemany(upsert_sql, params[start : start + UPSERT_BATCH])
            conn.commit()
    except psycopg.Error as exc:
        raise CrawlError(f"Postgres write failed: {_hide_dsn(str(exc), dsn)}") from None
    return len(prepared)


def load_csv_command(args: argparse.Namespace) -> int:
    if args.limit is not None and args.limit < 1:
        raise CrawlError("--limit must be a positive integer")
    dsn = database_dsn(args.dsn)
    if not dsn:
        raise CrawlError("Pass --dsn or set DATABASE_URL to load a CSV into Postgres")
    rows = read_csv_rows(args.csv, args.limit)
    saved = upsert_listings(dsn, rows, datetime.now(timezone.utc))
    print(f"Upserted {saved} rows into {LISTING_TABLE}", file=sys.stderr)
    return 0


def fetch_seller_page(client: Client, username: str, referer: str) -> Callable[[int, int], dict]:
    def fetch(page: int, page_size: int) -> dict:
        return client.get_json(
            API_ORIGIN + SELLER_API_PATH,
            {"user": username, "page": page, "pageSize": page_size},
            referer=referer,
        )

    return fetch


def fetch_search_page(client: Client, query: str) -> Callable[[int, int], dict]:
    def fetch(page: int, page_size: int) -> dict:
        return client.get_json(
            API_ORIGIN + SEARCH_API_PATH,
            {"page": page, "pageSize": page_size, "searchPhrase": query},
            referer=SITE_ORIGIN + "/",
        )

    return fetch


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help=f"Seconds between requests (minimum {DEFAULT_DELAY_SECONDS}).",
    )
    common.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help=f"Listings per request, 1 to {MAX_PAGE_SIZE}. The site itself asks for 200.",
    )
    common.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after this many listings. Omit to read every page.",
    )
    common.add_argument(
        "--dsn",
        default=None,
        help=(
            "Postgres URL. Listings are upserted into esam_listings as well as the CSV. "
            "If omitted, DATABASE_URL is used when set. The URL is never printed."
        ),
    )
    parser = argparse.ArgumentParser(
        description="Export public esam.ir listings to CSV."
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    seller = sub.add_parser(
        "seller", parents=[common], help="Export one seller's public listings."
    )
    seller.add_argument(
        "--url",
        help=(
            "Seller page URL. Verified example: "
            "https://esam.ir/aboutSeller?activeTab=items&orderusername=kolokolchik"
        ),
    )
    seller.add_argument(
        "--seller",
        help="Public seller username (the orderusername value). Numeric uids are not accepted by the listings API.",
    )
    seller.add_argument("--out", required=True, help="Output CSV path.")

    search = sub.add_parser(
        "search", parents=[common], help="Export public search results."
    )
    search.add_argument("--query", required=True, help="Search phrase.")
    search.add_argument("--out", required=True, help="Output CSV path.")

    load_csv = sub.add_parser(
        "load-csv",
        help="Upsert an existing listings CSV into Postgres. Does not crawl.",
    )
    load_csv.add_argument("--csv", required=True, help="CSV written by this crawler.")
    load_csv.add_argument(
        "--dsn",
        default=None,
        help="Postgres URL. If omitted, DATABASE_URL is used. The URL is never printed.",
    )
    load_csv.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Load only the first N data rows.",
    )
    return parser


def validate_common(args: argparse.Namespace) -> None:
    if args.page_size < 1 or args.page_size > MAX_PAGE_SIZE:
        raise CrawlError(f"--page-size must be between 1 and {MAX_PAGE_SIZE}")
    if args.limit is not None and args.limit < 1:
        raise CrawlError("--limit must be a positive integer")


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.mode == "load-csv":
            return load_csv_command(args)
        validate_common(args)
        dsn = database_dsn(args.dsn)
        client = Client(args.delay)
        if args.mode == "seller":
            if bool(args.url) == bool(args.seller):
                raise CrawlError("Pass exactly one of --url or --seller")
            if args.url:
                username = seller_username_from_url(args.url)
                client.ensure_allowed(args.url.strip())
            else:
                username = args.seller.strip()
                if not username:
                    raise CrawlError("--seller was empty")
            if username.isdigit():
                print(
                    "Note: the listings API keys sellers by username "
                    "(orderusername), not the numeric uid. A numeric value "
                    "usually returns no rows.",
                    file=sys.stderr,
                )
            referer = seller_page_url(username)
            client.ensure_allowed(referer)
            print(f"Seller username: {username}", file=sys.stderr)
            pages = iter_pages(
                fetch_seller_page(client, username, referer),
                args.limit,
                args.page_size,
            )
            rows = (row_from_item(item, query="") for item in pages)
        else:
            query = args.query.strip()
            if not query:
                raise CrawlError("--query was empty")
            # Do not request /search?kw= ; esam.ir robots.txt disallows it.
            pages = iter_pages(
                fetch_search_page(client, query),
                args.limit,
                args.page_size,
            )
            rows = (row_from_item(item, query=query) for item in pages)
        fetched_at = datetime.now(timezone.utc)
        saved_rows: list[dict[str, str]] = []

        def capture(source: Iterable[dict[str, str]]) -> Iterator[dict[str, str]]:
            for row in source:
                saved_rows.append(row)
                yield row

        count = write_csv(args.out, capture(rows))
        print(f"Wrote {count} rows to {args.out}", file=sys.stderr)
        if dsn:
            saved = upsert_listings(dsn, saved_rows, fetched_at)
            print(f"Upserted {saved} rows into {LISTING_TABLE}", file=sys.stderr)
    except CrawlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
