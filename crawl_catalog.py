import asyncio
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urldefrag

from app import BASE_URL, open_logged_in_page

CATEGORY_ROOT = os.getenv("MAKANU_CATEGORY_ROOT", "/produkty")
PAGE_TIMEOUT_MS = int(os.getenv("MAKANU_CRAWL_PAGE_TIMEOUT_MS", "60000"))
NETWORK_IDLE_TIMEOUT_MS = int(os.getenv("MAKANU_CRAWL_NETWORK_IDLE_TIMEOUT_MS", "7000"))
MAX_CATEGORIES = int(os.getenv("MAKANU_CRAWL_MAX_CATEGORIES", "200"))

PRICE_RE = re.compile(r"(\d{1,6}(?:[.,]\d{1,2})?)\s*(?:zł|PLN)", re.I)
CODE_AT_END_RE = re.compile(r"\s([A-Z0-9][A-Z0-9._/\-]{2,39})\s*$", re.I)


def emit(event, payload):
    print(json.dumps({
        "event": event,
        "ts": datetime.now(timezone.utc).isoformat(),
        **payload,
    }, ensure_ascii=False, separators=(",", ":")), flush=True)


def clean_url(url):
    url, _ = urldefrag(url)
    return url.strip()


def same_host(url):
    try:
        return urlparse(url).netloc == urlparse(BASE_URL).netloc
    except Exception:
        return False


def parse_float(value):
    if value is None:
        return None
    value = str(value).strip().replace(" ", "").replace(",", ".")
    try:
        return float(value)
    except ValueError:
        return None


def normalize_code(title, explicit_code):
    if explicit_code:
        return explicit_code.strip()
    m = CODE_AT_END_RE.search(title or "")
    return m.group(1).strip() if m else None


def normalize_stock(raw):
    text = " ".join([
        str(raw.get("availability_text") or ""),
        str(raw.get("card_text") or ""),
        str(raw.get("availability_src") or ""),
    ]).lower()

    qty = None
    for key in ("qty", "quantity", "stock", "available"):
        v = raw.get(key)
        if v is not None and str(v).strip().isdigit():
            qty = int(str(v).strip())
            break

    if "s_brak" in text:
        return "out_of_stock", 0
    if "s_duzo" in text:
        return "in_stock", qty
    if re.search(r"\b(brak|niedostęp|niedostep|out of stock|unavailable)\b", text):
        return "out_of_stock", 0
    if re.search(r"\b(dostęp|dostep|available|in stock)\b", text):
        return "in_stock", qty
    return "unknown", qty


async def discover_categories(page):
    await page.goto(
        urljoin(BASE_URL, CATEGORY_ROOT),
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT_MS,
    )
    try:
        await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
    except Exception:
        pass

    links = await page.locator("a").evaluate_all("""
        els => els.map(a => ({
            text: (a.innerText || "").trim(),
            href: a.href || ""
        }))
    """)

    categories = []
    seen = set()

    for item in links:
        href = clean_url(item.get("href") or "")
        if not href or not same_host(href):
            continue

        parts = urlparse(href).path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "products":
            continue
        if href in seen:
            continue

        seen.add(href)
        categories.append({
            "url": href,
            "brand": parts[1].replace("-", " "),
            "category": parts[2].replace("-", " "),
        })

    categories.sort(key=lambda x: (x["brand"].lower(), x["category"].lower()))
    return categories[:MAX_CATEGORIES]


async def extract_products_from_category(page, category):
    raw_products = await page.evaluate(r"""
        () => {
            const priceRe = /(\d{1,6}(?:[.,]\d{1,2})?)\s*(?:zł|PLN)/i;

            const clean = s => (s || "").replace(/\s+/g, " ").trim();

            function collectAttrs(el) {
                const out = {};
                if (!el) return out;
                for (const a of Array.from(el.attributes || [])) {
                    const k = a.name.toLowerCase();
                    if (
                        k.includes("stock") ||
                        k.includes("qty") ||
                        k.includes("quantity") ||
                        k.includes("available") ||
                        k.includes("product") ||
                        k.includes("code") ||
                        k.includes("sku")
                    ) out[k] = a.value;
                }
                return out;
            }

            function cardFromControl(control) {
                let node = control;
                let best = null;

                for (let depth = 0; node && depth < 8; depth++, node = node.parentElement) {
                    const t = clean(node.innerText || node.textContent || "");
                    if (!priceRe.test(t)) continue;

                    const links = Array.from(node.querySelectorAll("a"));
                    const imgs = Array.from(node.querySelectorAll("img"));
                    const inputs = Array.from(node.querySelectorAll("input"));

                    const score =
                        (imgs.some(img => (img.currentSrc || img.src || "").includes("/_data/products/")) ? 3 : 0) +
                        (links.length > 0 ? 1 : 0) +
                        (t.length < 2200 ? 2 : 0);

                    if (!best || score > best.score) best = {node, t, links, imgs, inputs, score};
                    if (score >= 5) break;
                }

                if (!best) return null;

                const priceMatch = best.t.match(priceRe);
                const image = best.imgs.find(img =>
                    (img.currentSrc || img.src || "").includes("/_data/products/")
                );
                const availabilityImage = best.imgs.find(img => {
                    const src = img.currentSrc || img.src || "";
                    return src.includes("s_duzo") || src.includes("s_brak");
                });

                const titleCandidates = best.links
                    .map(a => clean(a.innerText || a.textContent || ""))
                    .filter(v =>
                        v &&
                        !/^add to cart$/i.test(v) &&
                        !/^home$/i.test(v) &&
                        !/^[0-9]+$/.test(v) &&
                        !priceRe.test(v)
                    )
                    .sort((a,b) => b.length - a.length);

                let title = titleCandidates[0] || "";
                if (!title) {
                    const lines = (best.node.innerText || "")
                        .split(/\n+/)
                        .map(v => v.trim())
                        .filter(Boolean);
                    title = lines.find(v =>
                        !priceRe.test(v) &&
                        !/^availability:?$/i.test(v) &&
                        !/^szt\.?$/i.test(v) &&
                        !/^add to cart$/i.test(v)
                    ) || "";
                }

                let href = "";
                for (const a of best.links) {
                    const at = clean(a.innerText || a.textContent || "");
                    if (at === title) {
                        href = a.href || "";
                        break;
                    }
                }

                const attrs = {...collectAttrs(best.node), ...collectAttrs(control)};
                const explicitCode =
                    attrs["data-sku"] ||
                    attrs["data-code"] ||
                    attrs["data-product-code"] ||
                    attrs["sku"] ||
                    attrs["code"] ||
                    "";

                let qty = null, quantity = null, stock = null, available = null;
                for (const inp of best.inputs) {
                    const name = (inp.name || inp.id || "").toLowerCase();
                    const value = inp.value;
                    if (name.includes("qty")) qty = value;
                    if (name.includes("quantity")) quantity = value;
                    if (name.includes("stock")) stock = value;
                    if (name.includes("available")) available = value;
                }

                return {
                    title,
                    product_href: href,
                    image_url: image ? (image.currentSrc || image.src || "") : "",
                    availability_src: availabilityImage ? (availabilityImage.currentSrc || availabilityImage.src || "") : "",
                    price_text: priceMatch ? priceMatch[1] : "",
                    explicit_code: explicitCode,
                    qty,
                    quantity,
                    stock,
                    available,
                    availability_text: best.t,
                    card_text: best.t.slice(0, 1200),
                };
            }

            const controls = Array.from(
                document.querySelectorAll('button, input[type="submit"], input[type="button"], a')
            ).filter(el => {
                const v = clean(el.innerText || el.value || el.textContent || "");
                return /add to cart/i.test(v);
            });

            const results = [];
            const seen = new Set();

            for (const control of controls) {
                const item = cardFromControl(control);
                if (!item) continue;

                const key = [item.title, item.price_text, item.product_href, item.image_url].join("|");
                if (seen.has(key)) continue;
                seen.add(key);
                results.push(item);
            }

            return results;
        }
    """)

    products = []
    for raw in raw_products:
        title = (raw.get("title") or "").strip()
        price_pln = parse_float(raw.get("price_text"))

        if not title or price_pln is None:
            continue

        stock_status, stock_qty = normalize_stock(raw)

        products.append({
            "brand": category["brand"],
            "category": category["category"],
            "title": title,
            "supplier_sku": normalize_code(title, raw.get("explicit_code")),
            "ean": None,
            "wholesale_price_pln": price_pln,
            "stock_status": stock_status,
            "stock_qty": stock_qty,
            "source_url": raw.get("product_href") or category["url"],
            "category_url": category["url"],
            "image_url": raw.get("image_url") or None,
            "availability_icon": raw.get("availability_src") or None,
        })

    return products


async def main():
    playwright = browser = context = None

    try:
        playwright, browser, context, page = await open_logged_in_page()
        categories = await discover_categories(page)

        emit("crawl_start", {
            "mode": "category_fast",
            "categories_found": len(categories),
        })

        total_products = 0
        in_stock = 0
        out_of_stock = 0
        unknown_stock = 0
        category_errors = 0
        emitted = set()

        for index, category in enumerate(categories, start=1):
            emit("category_begin", {
                "category_number": index,
                "categories_total": len(categories),
                "brand": category["brand"],
                "category": category["category"],
                "url": category["url"],
            })

            try:
                await page.goto(
                    category["url"],
                    wait_until="domcontentloaded",
                    timeout=PAGE_TIMEOUT_MS,
                )

                try:
                    await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
                except Exception:
                    pass

                products = await extract_products_from_category(page, category)
                found_here = 0

                for product in products:
                    key = (
                        product.get("supplier_sku"),
                        product.get("title"),
                        product.get("wholesale_price_pln"),
                        product.get("image_url"),
                    )
                    if key in emitted:
                        continue
                    emitted.add(key)

                    total_products += 1
                    found_here += 1

                    if product["stock_status"] == "in_stock":
                        in_stock += 1
                    elif product["stock_status"] == "out_of_stock":
                        out_of_stock += 1
                    else:
                        unknown_stock += 1

                    emit("product", {"product": product})

                emit("category_done", {
                    "category_number": index,
                    "categories_total": len(categories),
                    "brand": category["brand"],
                    "category": category["category"],
                    "products_found": found_here,
                    "total_products": total_products,
                })

            except Exception as exc:
                category_errors += 1
                emit("category_error", {
                    "category_number": index,
                    "categories_total": len(categories),
                    "brand": category["brand"],
                    "category": category["category"],
                    "url": category["url"],
                    "error_type": type(exc).__name__,
                })

        emit("crawl_summary", {
            "ok": category_errors == 0,
            "mode": "category_fast",
            "categories_found": len(categories),
            "categories_with_errors": category_errors,
            "products_found": total_products,
            "in_stock_products": in_stock,
            "out_of_stock_products": out_of_stock,
            "unknown_stock_products": unknown_stock,
        })

    except Exception as exc:
        emit("crawl_fatal", {
            "ok": False,
            "mode": "category_fast",
            "error_type": type(exc).__name__,
        })
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
