import asyncio
import json
import os
import re
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlparse, urldefrag

from app import BASE_URL, open_logged_in_page


MAX_PAGES = int(os.getenv("MAKANU_CRAWL_MAX_PAGES", "400"))
MAX_LINKS_PER_PAGE = int(os.getenv("MAKANU_CRAWL_MAX_LINKS_PER_PAGE", "1000"))
PAGE_TIMEOUT_MS = int(os.getenv("MAKANU_CRAWL_PAGE_TIMEOUT_MS", "60000"))
NETWORK_IDLE_TIMEOUT_MS = int(os.getenv("MAKANU_CRAWL_NETWORK_IDLE_TIMEOUT_MS", "8000"))

PRODUCT_HINTS = (
    "produkt",
    "product",
    "towar",
    "katalog",
    "catalog",
    "oferta",
    "shop",
    "sklep",
    "magazyn",
    "stock",
)

SKIP_HINTS = (
    "logout",
    "wyloguj",
    "konto",
    "account",
    "profil",
    "profile",
    "password",
    "haslo",
    "koszyk",
    "cart",
    "checkout",
    "zamow",
    "order",
    "faktura",
    "invoice",
)

PRICE_RE = re.compile(
    r"(?:(?:cena|price)[^\d]{0,20})?"
    r"(\d{1,6}(?:[.,]\d{1,2})?)\s*(?:zł|pln)",
    re.IGNORECASE,
)

EAN_RE = re.compile(r"\b(\d{8}|\d{12,14})\b")

SKU_PATTERNS = (
    re.compile(r"(?:sku|kod(?:\s+produktu)?|indeks|symbol)\s*[:#]?\s*([A-Z0-9._/\-]{3,40})", re.I),
)

STOCK_PATTERNS = (
    re.compile(r"(?:stan(?:\s+magazynowy)?|stock|ilość|ilosc|dostępne|dostepne)\s*[:#]?\s*(\d+)", re.I),
    re.compile(r"(\d+)\s*(?:szt\.?|sztuk)\b", re.I),
)

BRAND_PATTERNS = (
    re.compile(r"(?:marka|brand|producent)\s*[:#]?\s*([^\n\r|]{2,80})", re.I),
)


def emit(event: str, payload: dict):
    print(
        json.dumps(
            {
                "event": event,
                "ts": datetime.now(timezone.utc).isoformat(),
                **payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )


def same_host(url: str) -> bool:
    try:
        return urlparse(url).netloc == urlparse(BASE_URL).netloc
    except Exception:
        return False


def clean_url(url: str) -> str:
    url, _ = urldefrag(url)
    return url.strip()


def score_link(text: str, href: str) -> int:
    hay = f"{text} {href}".lower()

    if any(x in hay for x in SKIP_HINTS):
        return -100

    score = 0
    for hint in PRODUCT_HINTS:
        if hint in hay:
            score += 5

    if "/produkt" in hay or "/product" in hay:
        score += 20

    return score


def first_match(patterns, text: str):
    for pattern in patterns:
        m = pattern.search(text)
        if m:
            return m.group(1).strip()
    return None


def parse_number(value: str | None):
    if value is None:
        return None
    value = value.replace(" ", "").replace(",", ".")
    try:
        return float(value)
    except ValueError:
        return None


def parse_page_candidate(url: str, title: str, text: str, images: list[str]) -> dict | None:
    sku = first_match(SKU_PATTERNS, text)
    ean_match = EAN_RE.search(text)
    ean = ean_match.group(1) if ean_match else None

    price_match = PRICE_RE.search(text)
    wholesale_pln = parse_number(price_match.group(1)) if price_match else None

    stock_raw = first_match(STOCK_PATTERNS, text)
    stock = int(stock_raw) if stock_raw and stock_raw.isdigit() else None

    brand = first_match(BRAND_PATTERNS, text)

    signals = sum(
        x is not None
        for x in (sku, ean, wholesale_pln, stock, brand)
    )

    productish_url = any(x in url.lower() for x in ("/produkt", "/product", "towar"))
    if signals < 2 and not (productish_url and wholesale_pln is not None):
        return None

    return {
        "source_url": url,
        "title": title.strip(),
        "brand": brand,
        "supplier_sku": sku,
        "ean": ean,
        "wholesale_price_pln": wholesale_pln,
        "stock_qty": stock,
        "images": images[:10],
        "text_excerpt": text[:4000],
    }


def looks_like_product_dict(obj: dict) -> bool:
    keys = {str(k).lower() for k in obj.keys()}

    groups = [
        {"sku", "kod", "code", "symbol", "indeks", "index"},
        {"ean", "barcode", "gtin"},
        {"price", "cena", "netto", "brutto", "wholesale_price"},
        {"stock", "qty", "quantity", "ilosc", "ilość", "available", "availability"},
        {"name", "title", "nazwa", "product_name"},
    ]

    hits = sum(bool(keys & group) for group in groups)
    return hits >= 3


def sanitize_product_dict(obj: dict) -> dict:
    blocked = (
        "password",
        "passwd",
        "token",
        "secret",
        "cookie",
        "authorization",
        "session",
        "csrf",
    )

    result = {}
    for key, value in obj.items():
        key_str = str(key)
        if any(b in key_str.lower() for b in blocked):
            continue

        if isinstance(value, (str, int, float, bool)) or value is None:
            text = str(value) if isinstance(value, str) else value
            if isinstance(text, str) and len(text) > 2000:
                text = text[:2000]
            result[key_str] = text

    return result


def walk_json(node):
    if isinstance(node, dict):
        if looks_like_product_dict(node):
            yield sanitize_product_dict(node)

        for value in node.values():
            yield from walk_json(value)

    elif isinstance(node, list):
        for value in node:
            yield from walk_json(value)


async def main():
    playwright = None
    browser = None
    context = None

    seen_pages = set()
    emitted_page_products = set()
    emitted_api_products = set()
    captured_api_products = []

    try:
        playwright, browser, context, page = await open_logged_in_page()

        start_url = clean_url(page.url)
        base_host = urlparse(BASE_URL).netloc

        emit(
            "crawl_start",
            {
                "base_url": BASE_URL,
                "start_url": start_url,
                "max_pages": MAX_PAGES,
            },
        )

        async def handle_response(response):
            try:
                url = response.url
                if urlparse(url).netloc != base_host:
                    return

                content_type = (response.headers.get("content-type") or "").lower()
                if "application/json" not in content_type and "json" not in content_type:
                    return

                data = await response.json()

                for item in walk_json(data):
                    key = json.dumps(item, ensure_ascii=False, sort_keys=True)
                    if key in emitted_api_products:
                        continue

                    emitted_api_products.add(key)
                    captured_api_products.append(item)

                    emit(
                        "api_product",
                        {
                            "response_url": url,
                            "product": item,
                        },
                    )
            except Exception:
                return

        page.on("response", handle_response)

        queue = deque([start_url])

        while queue and len(seen_pages) < MAX_PAGES:
            url = queue.popleft()

            if url in seen_pages:
                continue
            if not same_host(url):
                continue
            if any(x in url.lower() for x in SKIP_HINTS):
                continue

            seen_pages.add(url)

            emit(
                "page_begin",
                {
                    "page_number": len(seen_pages),
                    "url": url,
                },
            )

            try:
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=PAGE_TIMEOUT_MS,
                )
            except Exception as exc:
                emit(
                    "page_error",
                    {
                        "url": url,
                        "error_type": type(exc).__name__,
                    },
                )
                continue

            try:
                await page.wait_for_load_state(
                    "networkidle",
                    timeout=NETWORK_IDLE_TIMEOUT_MS,
                )
            except Exception:
                pass

            try:
                title = await page.title()
                text = await page.locator("body").inner_text(timeout=15000)
            except Exception as exc:
                emit(
                    "page_parse_error",
                    {
                        "url": page.url,
                        "error_type": type(exc).__name__,
                    },
                )
                continue

            try:
                images = await page.locator("img").evaluate_all(
                    """
                    els => els
                        .map(img => img.currentSrc || img.src || "")
                        .filter(Boolean)
                        .slice(0, 50)
                    """
                )
            except Exception:
                images = []

            candidate = parse_page_candidate(
                page.url,
                title,
                text,
                images,
            )

            if candidate:
                candidate_key = (
                    candidate.get("supplier_sku"),
                    candidate.get("ean"),
                    candidate.get("source_url"),
                )

                if candidate_key not in emitted_page_products:
                    emitted_page_products.add(candidate_key)
                    emit("page_product", {"product": candidate})

            try:
                links = await page.locator("a").evaluate_all(
                    """
                    els => els.slice(0, %d).map(a => ({
                        text: (a.innerText || "").trim(),
                        href: a.href || ""
                    }))
                    """
                    % MAX_LINKS_PER_PAGE
                )
            except Exception:
                links = []

            ranked = []
            for item in links:
                href = clean_url(item.get("href") or "")
                text_value = item.get("text") or ""

                if not href or not same_host(href):
                    continue
                if href in seen_pages:
                    continue

                score = score_link(text_value, href)
                if score <= -100:
                    continue

                ranked.append((score, href))

            ranked.sort(key=lambda x: x[0], reverse=True)

            for _, href in ranked:
                if href not in queue:
                    queue.append(href)

            emit(
                "page_done",
                {
                    "url": page.url,
                    "queue_size": len(queue),
                    "seen_pages": len(seen_pages),
                    "page_products": len(emitted_page_products),
                    "api_products": len(emitted_api_products),
                },
            )

        emit(
            "crawl_summary",
            {
                "ok": True,
                "pages_visited": len(seen_pages),
                "page_products_found": len(emitted_page_products),
                "api_products_found": len(emitted_api_products),
                "total_product_candidates": (
                    len(emitted_page_products) + len(emitted_api_products)
                ),
            },
        )

    except Exception as exc:
        emit(
            "crawl_fatal",
            {
                "ok": False,
                "error_type": type(exc).__name__,
            },
        )
        raise

    finally:
        if context:
            await context.close()

        if browser:
            await browser.close()

        if playwright:
            await playwright.stop()


if __name__ == "__main__":
    asyncio.run(main())
