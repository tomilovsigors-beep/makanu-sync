import asyncio
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from app import BASE_URL, open_logged_in_page

PAGE_TIMEOUT_MS = int(os.getenv("MAKANU_CRAWL_PAGE_TIMEOUT_MS", "60000"))
NETWORK_IDLE_TIMEOUT_MS = int(os.getenv("MAKANU_CRAWL_NETWORK_IDLE_TIMEOUT_MS", "7000"))

XML_PAGE = "/xml.html"
KNOWN_XML_FEED = "/p/Exporter/Index/xml"

ALIASES = {
    "sku": ("sku","symbol","code","kod","productcode","product_code","indeks","index"),
    "ean": ("ean","ean13","barcode","gtin","gtin13"),
    "title": ("name","title","productname","product_name","nazwa"),
    "brand": ("brand","producer","manufacturer","marka","producent"),
    "category": ("category","categoryname","category_name","kategoria"),
    "price": ("price","netprice","net_price","wholesaleprice","wholesale_price","cena","cenanetto","cena_netto"),
    "stock": ("stock","quantity","qty","availability","available","stan","stanmagazynowy","stan_magazynowy","ilosc","ilość","magazyn"),
    "image": ("image","imageurl","image_url","photo","picture","img","zdjecie","zdjęcie"),
    "url": ("url","link","producturl","product_url","href"),
}

STOCK_HINTS = (
    "stock","qty","quantity","availability","available",
    "stan","magazyn","ilosc","ilość","inventory"
)

EXPORT_HINTS = (
    "xml","csv","xls","xlsx","export","feed","offer","oferta"
)


def emit(event, payload):
    print(json.dumps({
        "event": event,
        "ts": datetime.now(timezone.utc).isoformat(),
        **payload
    }, ensure_ascii=False, separators=(",", ":")), flush=True)


def clean(v):
    return re.sub(r"\s+", " ", str(v or "")).strip()


def local_name(tag):
    return str(tag).split("}", 1)[-1].strip().lower()


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
        score += 3 if first(flat, ALIASES["stock"]) else 0
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
    source = first(flat, ALIASES["url"]) or feed_url
    image = first(flat, ALIASES["image"]) or None
    if source:
        source = urljoin(BASE_URL, source)
    if image:
        image = urljoin(BASE_URL, image)
    return {
        "brand": first(flat, ALIASES["brand"]) or None,
        "category": first(flat, ALIASES["category"]) or None,
        "title": first(flat, ALIASES["title"]) or None,
        "supplier_sku": first(flat, ALIASES["sku"]) or None,
        "ean": first(flat, ALIASES["ean"]) or None,
        "wholesale_price_pln": parse_number(first(flat, ALIASES["price"])),
        "stock_qty": parse_stock(first(flat, ALIASES["stock"])),
        "stock_raw": first(flat, ALIASES["stock"]) or None,
        "source_url": source,
        "image_url": image,
        "xml_feed_url": feed_url,
    }


async def fetch(context, url, method="GET", data=None):
    try:
        if method.upper() == "POST":
            r = await context.request.post(
                url,
                form=data or {},
                timeout=PAGE_TIMEOUT_MS,
                fail_on_status_code=False,
            )
        else:
            r = await context.request.get(
                url,
                timeout=PAGE_TIMEOUT_MS,
                fail_on_status_code=False,
            )
        return {
            "ok": r.ok,
            "status": r.status,
            "url": r.url,
            "content_type": r.headers.get("content-type", ""),
            "text": await r.text(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": None,
            "url": url,
            "content_type": "",
            "text": "",
            "error": f"{type(exc).__name__}: {exc}",
        }


def analyze_text_for_stock(text):
    low = (text or "").lower()
    found = [h for h in STOCK_HINTS if h in low]
    return sorted(set(found))


async def inspect_xml_page(page):
    url = urljoin(BASE_URL, XML_PAGE)
    await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    try:
        await page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
    except Exception:
        pass

    data = await page.evaluate("""
    () => {
      const forms = [...document.forms].map(f => ({
        action: f.action || "",
        method: (f.method || "GET").toUpperCase(),
        inputs: [...f.elements].map(e => ({
          tag: e.tagName,
          type: e.type || "",
          name: e.name || "",
          value: e.value || "",
          checked: !!e.checked
        })).slice(0,200)
      }));

      const links = [...document.querySelectorAll('a[href]')].map(a => ({
        text: (a.innerText || a.textContent || "").trim(),
        href: a.href || ""
      }));

      const scripts = [...document.scripts].map(s => ({
        src: s.src || "",
        text: (s.src ? "" : (s.textContent || "")).slice(0,10000)
      }));

      const selects = [...document.querySelectorAll('select')].map(s => ({
        name: s.name || "",
        id: s.id || "",
        options: [...s.options].map(o => ({
          text: (o.textContent || "").trim(),
          value: o.value || "",
          selected: !!o.selected
        }))
      }));

      return {
        title: document.title,
        url: location.href,
        bodyText: (document.body.innerText || "").slice(0,30000),
        forms,
        links,
        scripts,
        selects
      };
    }
    """)

    interesting_links = []
    for item in data.get("links", []):
        probe = f"{item.get('text','')} {item.get('href','')}".lower()
        if any(h in probe for h in EXPORT_HINTS + STOCK_HINTS):
            interesting_links.append(item)

    interesting_scripts = []
    for item in data.get("scripts", []):
        probe = f"{item.get('src','')} {item.get('text','')}".lower()
        if any(h in probe for h in EXPORT_HINTS + STOCK_HINTS):
            interesting_scripts.append({
                "src": item.get("src"),
                "text_excerpt": clean(item.get("text"))[:3000],
            })

    emit("xml_page_diagnostics", {
        "url": data.get("url"),
        "title": data.get("title"),
        "body_stock_hints": analyze_text_for_stock(data.get("bodyText","")),
        "forms": data.get("forms", []),
        "selects": data.get("selects", []),
        "interesting_links": interesting_links[:100],
        "interesting_scripts": interesting_scripts[:30],
    })

    candidates = []

    for item in interesting_links:
        href = item.get("href") or ""
        if href and same_host(href):
            candidates.append(("GET", href, None, "link"))

    for form in data.get("forms", []):
        action = form.get("action") or data.get("url")
        method = (form.get("method") or "GET").upper()
        if not action or not same_host(action):
            continue

        defaults = {}
        for inp in form.get("inputs", []):
            name = inp.get("name") or ""
            typ = (inp.get("type") or "").lower()
            if not name:
                continue
            if typ in {"checkbox","radio"} and not inp.get("checked"):
                continue
            defaults[name] = inp.get("value") or ""

        candidates.append((method, action, defaults, "form_default"))

        # Try exporter select options by substituting one option at a time.
        for sel in data.get("selects", []):
            name = sel.get("name") or ""
            if not name:
                continue
            for opt in sel.get("options", []):
                value = opt.get("value") or ""
                label = opt.get("text") or ""
                probe = f"{value} {label}".lower()
                if any(h in probe for h in EXPORT_HINTS + STOCK_HINTS):
                    payload = dict(defaults)
                    payload[name] = value
                    candidates.append((method, action, payload, f"select:{name}={value}"))

    # Known full XML feed.
    candidates.append(("GET", urljoin(BASE_URL, KNOWN_XML_FEED), None, "known_xml"))

    deduped = []
    seen = set()
    for method, url, payload, origin in candidates:
        key = (method, url, json.dumps(payload or {}, sort_keys=True, ensure_ascii=False))
        if key in seen:
            continue
        seen.add(key)
        deduped.append((method, url, payload, origin))

    return deduped[:60]


async def main():
    playwright = browser = context = None
    try:
        playwright, browser, context, page = await open_logged_in_page()
        candidates = await inspect_xml_page(page)

        emit("stock_probe_start", {
            "candidate_count": len(candidates),
        })

        best_products = []
        best_url = None
        best_stock_count = 0

        for method, url, payload, origin in candidates:
            result = await fetch(context, url, method, payload)
            text = result.get("text") or ""
            ctype = (result.get("content_type") or "").lower()
            hints = analyze_text_for_stock(text)

            emit("stock_probe_candidate", {
                "origin": origin,
                "method": method,
                "url": url,
                "status": result.get("status"),
                "content_type": result.get("content_type"),
                "bytes": len(text),
                "stock_hints": hints,
            })

            low = text[:20000].lower()
            looks_xml = (
                text.lstrip().startswith("<")
                and sum(h in low for h in ("<product","<item","<offer","<towar","<produkt","<name","<nazwa","<sku","<ean")) >= 2
            )

            if not result.get("ok") or not looks_xml:
                continue

            try:
                root = ET.fromstring(text)
            except ET.ParseError:
                continue

            elems = choose_product_elements(root)
            products = []
            stock_count = 0

            for _, flat in elems:
                p = normalize(flat, result.get("url") or url)
                if not (p["title"] or p["supplier_sku"] or p["ean"]):
                    continue
                products.append(p)
                if p["stock_qty"] is not None:
                    stock_count += 1

            emit("stock_probe_xml_result", {
                "url": result.get("url") or url,
                "products": len(products),
                "products_with_stock": stock_count,
                "sample_stock": [
                    {
                        "sku": p.get("supplier_sku"),
                        "stock_qty": p.get("stock_qty"),
                        "stock_raw": p.get("stock_raw"),
                    }
                    for p in products if p.get("stock_qty") is not None
                ][:10],
            })

            if len(products) > len(best_products) or stock_count > best_stock_count:
                best_products = products
                best_url = result.get("url") or url
                best_stock_count = stock_count

        if best_products:
            for p in best_products:
                emit("product", {"product": p})

        emit("crawl_summary", {
            "ok": bool(best_products),
            "mode": "stock-discovery",
            "best_feed_url": best_url,
            "products_found": len(best_products),
            "products_with_stock": best_stock_count,
            "stock_source_found": best_stock_count > 0,
        })

    except Exception as exc:
        emit("crawl_fatal", {
            "ok": False,
            "mode": "stock-discovery",
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
