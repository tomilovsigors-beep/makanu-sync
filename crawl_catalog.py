import asyncio
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, urldefrag

from app import BASE_URL, open_logged_in_page

PAGE_TIMEOUT_MS = int(os.getenv("MAKANU_CRAWL_PAGE_TIMEOUT_MS", "60000"))
NETWORK_IDLE_TIMEOUT_MS = int(os.getenv("MAKANU_CRAWL_NETWORK_IDLE_TIMEOUT_MS", "7000"))
MAX_CANDIDATES = int(os.getenv("MAKANU_XML_MAX_CANDIDATES", "30"))
MAX_PRODUCTS = int(os.getenv("MAKANU_XML_MAX_PRODUCTS", "20000"))

ALIASES = {
    "sku": ("sku","symbol","code","kod","productcode","product_code","indeks","index"),
    "ean": ("ean","ean13","barcode","gtin","gtin13"),
    "title": ("name","title","productname","product_name","nazwa"),
    "brand": ("brand","producer","manufacturer","marka","producent"),
    "category": ("category","categoryname","category_name","kategoria"),
    "price": ("price","netprice","net_price","wholesaleprice","wholesale_price","cena","cenanetto","cena_netto"),
    "stock": ("stock","quantity","qty","availability","available","stan","stanmagazynowy","stan_magazynowy","ilosc","ilość"),
    "image": ("image","imageurl","image_url","photo","picture","img","zdjecie","zdjęcie"),
    "url": ("url","link","producturl","product_url","href"),
}


def emit(event, payload):
    print(json.dumps({
        "event": event,
        "ts": datetime.now(timezone.utc).isoformat(),
        **payload
    }, ensure_ascii=False, separators=(",", ":")), flush=True)


def local_name(tag):
    return str(tag).split("}", 1)[-1].strip().lower()


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def same_host(url):
    try:
        return urlparse(url).netloc == urlparse(BASE_URL).netloc
    except Exception:
        return False


def parse_number(value):
    s = clean(value).replace("\xa0", " ").replace("PLN", "").replace("zł", "")
    s = re.sub(r"[^0-9,.-]", "", s)
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_stock(value):
    s = clean(value).lower()
    if not s:
        return None
    if s in {"true","yes","tak","available","in stock","dostępny","dostepny"}:
        return 1
    if s in {"false","no","nie","unavailable","out of stock","brak","niedostępny","niedostepny"}:
        return 0
    n = parse_number(s)
    return max(0, int(n)) if n is not None else None


def flatten(elem):
    out = {}
    for k, v in elem.attrib.items():
        if clean(v):
            out.setdefault(local_name(k), clean(v))
    for child in list(elem):
        key = local_name(child.tag)
        if len(list(child)) == 0 and clean(child.text):
            out.setdefault(key, clean(child.text))
        for k, v in child.attrib.items():
            if clean(v):
                out.setdefault(f"{key}_{local_name(k)}", clean(v))
    return out


def first(flat, aliases):
    for a in aliases:
        if clean(flat.get(a)):
            return clean(flat[a])
    return ""


def looks_like_xml_catalog(text):
    low = (text or "")[:15000].lower()
    if not low.lstrip().startswith("<"):
        return False
    hints = ("<product","<item","<offer","<towar","<produkt","<name","<nazwa","<price","<cena","<sku","<ean")
    return sum(h in low for h in hints) >= 2


def choose_product_elements(root):
    scored = []
    for elem in root.iter():
        if not list(elem):
            continue
        flat = flatten(elem)
        score = 0
        score += 3 if first(flat, ALIASES["title"]) else 0
        score += 3 if first(flat, ALIASES["sku"]) else 0
        score += 2 if first(flat, ALIASES["ean"]) else 0
        score += 3 if first(flat, ALIASES["price"]) else 0
        score += 2 if first(flat, ALIASES["stock"]) else 0
        if any(x in local_name(elem.tag) for x in ("product","item","offer","towar","produkt")):
            score += 2
        if score >= 6:
            scored.append((score, local_name(elem.tag), elem, flat))
    if not scored:
        return []
    best = max(x[0] for x in scored)
    chosen = [x for x in scored if x[0] >= max(6, best - 2)]
    counts = {}
    for _, tag, _, _ in chosen:
        counts[tag] = counts.get(tag, 0) + 1
    if counts:
        tag, count = max(counts.items(), key=lambda x: x[1])
        if count >= 3:
            chosen = [x for x in chosen if x[1] == tag]
    return [(elem, flat) for _, _, elem, flat in chosen]


def normalize(flat, feed_url):
    p = {
        "brand": first(flat, ALIASES["brand"]) or None,
        "category": first(flat, ALIASES["category"]) or None,
        "title": first(flat, ALIASES["title"]) or None,
        "supplier_sku": first(flat, ALIASES["sku"]) or None,
        "ean": first(flat, ALIASES["ean"]) or None,
        "wholesale_price_pln": parse_number(first(flat, ALIASES["price"])),
        "stock_qty": parse_stock(first(flat, ALIASES["stock"])),
        "stock_raw": first(flat, ALIASES["stock"]) or None,
        "source_url": first(flat, ALIASES["url"]) or feed_url,
        "image_url": first(flat, ALIASES["image"]) or None,
        "xml_feed_url": feed_url,
    }
    if p["source_url"]:
        p["source_url"] = urljoin(BASE_URL, p["source_url"])
    if p["image_url"]:
        p["image_url"] = urljoin(BASE_URL, p["image_url"])
    return p


async def fetch(context, url):
    try:
        r = await context.request.get(url, timeout=PAGE_TIMEOUT_MS, fail_on_status_code=False)
        return {
            "ok": r.ok,
            "status": r.status,
            "url": r.url,
            "content_type": r.headers.get("content-type", ""),
            "text": await r.text(),
        }
    except Exception as exc:
        return {"ok": False, "status": None, "url": url, "content_type": "", "text": "", "error": type(exc).__name__}


async def discover_links(page, url):
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        try:
            await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
        except Exception:
            pass
        links = await page.locator("a").evaluate_all("""
            els => els.map(a => ({
                text:(a.innerText || a.textContent || "").trim(),
                href:a.href || ""
            }))
        """)
    except Exception:
        return []

    out = []
    for item in links:
        href, _ = urldefrag(item.get("href") or "")
        label = clean(item.get("text"))
        probe = f"{label} {href}".lower()
        if href and same_host(href) and (
            "xml" in probe or "feed" in probe or "export" in probe or
            href.lower().endswith(".xml") or "format=xml" in href.lower()
        ):
            out.append({"url": href, "label": label})
    return out


async def main():
    playwright = browser = context = None
    try:
        playwright, browser, context, page = await open_logged_in_page()

        queue = await discover_links(page, urljoin(BASE_URL, "/pulpit"))
        for path in ("/xml", "/XML", "/oferta", "/export", "/feed"):
            queue.append({"url": urljoin(BASE_URL, path), "label": path})

        emit("xml_discovery_start", {"candidates_found": len(queue), "candidates": queue[:30]})

        seen = set()
        products = []
        product_keys = set()

        while queue and len(seen) < MAX_CANDIDATES:
            item = queue.pop(0)
            url = item["url"]
            if not url or url in seen:
                continue
            seen.add(url)

            result = await fetch(context, url)
            text = result.get("text") or ""
            is_xml = looks_like_xml_catalog(text)

            emit("xml_candidate", {
                "url": url,
                "label": item.get("label"),
                "status": result.get("status"),
                "content_type": result.get("content_type"),
                "bytes": len(text),
                "looks_like_xml_catalog": is_xml,
            })

            if result.get("ok") and is_xml:
                try:
                    root = ET.fromstring(text)
                except ET.ParseError as exc:
                    emit("xml_parse_error", {"url": url, "error": str(exc)[:300]})
                    continue

                elems = choose_product_elements(root)
                emit("xml_schema", {
                    "url": result.get("url") or url,
                    "root_tag": local_name(root.tag),
                    "candidate_elements": len(elems),
                })

                for _, flat in elems[:MAX_PRODUCTS]:
                    p = normalize(flat, result.get("url") or url)
                    if not (p["title"] or p["supplier_sku"] or p["ean"]):
                        continue
                    if p["wholesale_price_pln"] is None and p["stock_qty"] is None:
                        continue

                    key = (p["supplier_sku"], p["ean"], p["title"], p["source_url"])
                    if key in product_keys:
                        continue
                    product_keys.add(key)
                    products.append(p)
                    emit("product", {"product": p})

                if products:
                    emit("crawl_summary", {
                        "ok": True,
                        "mode": "xml",
                        "xml_feed_url": result.get("url") or url,
                        "xml_candidates_checked": len(seen),
                        "products_found": len(products),
                        "products_with_sku": sum(bool(p["supplier_sku"]) for p in products),
                        "products_with_ean": sum(bool(p["ean"]) for p in products),
                        "products_with_price": sum(p["wholesale_price_pln"] is not None for p in products),
                        "products_with_stock": sum(p["stock_qty"] is not None for p in products),
                        "zero_stock_products": sum(p["stock_qty"] == 0 for p in products),
                    })
                    return

            if result.get("ok") and "html" in (result.get("content_type") or "").lower():
                for extra in await discover_links(page, result.get("url") or url):
                    if extra["url"] not in seen:
                        queue.append(extra)

        emit("crawl_summary", {
            "ok": False,
            "mode": "xml",
            "xml_candidates_checked": len(seen),
            "products_found": len(products),
            "reason": "No parseable product XML feed found",
        })

    except Exception as exc:
        emit("crawl_fatal", {
            "ok": False,
            "mode": "xml",
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
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
