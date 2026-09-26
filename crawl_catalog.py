import asyncio
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from app import BASE_URL, open_logged_in_page

PAGE_TIMEOUT_MS = int(os.getenv("MAKANU_CRAWL_PAGE_TIMEOUT_MS", "60000"))
NETWORK_IDLE_TIMEOUT_MS = int(os.getenv("MAKANU_CRAWL_NETWORK_IDLE_TIMEOUT_MS", "10000"))

TARGETS = [
    "/products/",
    "/products?mi=1041",
    "/products/yak/odziez-kajakowa?page=1&perpage=30&so=Code",
]

HINTS = (
    "stock", "qty", "quantity", "availability", "available",
    "stan", "magazyn", "inventory", "product", "cart", "price"
)


def emit(event, payload):
    print(json.dumps({
        "event": event,
        "ts": datetime.now(timezone.utc).isoformat(),
        **payload
    }, ensure_ascii=False, separators=(",", ":")), flush=True)


def interesting(url, content_type):
    probe = f"{url} {content_type}".lower()
    return any(h in probe for h in HINTS)


async def main():
    playwright = browser = context = None
    try:
        playwright, browser, context, page = await open_logged_in_page()

        captured = []

        async def on_response(response):
            try:
                url = response.url
                ctype = response.headers.get("content-type", "")
                rtype = response.request.resource_type

                if rtype not in {"xhr", "fetch", "document"}:
                    return

                if not interesting(url, ctype):
                    return

                item = {
                    "url": url,
                    "status": response.status,
                    "content_type": ctype,
                    "resource_type": rtype,
                    "method": response.request.method,
                    "post_data": response.request.post_data,
                }

                try:
                    body = await response.text()
                except Exception:
                    body = ""

                if body:
                    item["body_excerpt"] = body[:8000]

                captured.append(item)
                emit("network_candidate", item)

            except Exception as exc:
                emit("network_probe_error", {
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:300],
                })

        page.on("response", on_response)

        for path in TARGETS:
            url = urljoin(BASE_URL, path)

            emit("network_probe_page", {"url": url})

            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT_MS,
            )

            try:
                await page.wait_for_load_state(
                    "networkidle",
                    timeout=NETWORK_IDLE_TIMEOUT_MS,
                )
            except Exception:
                pass

            # Force lazy-loaded product widgets and availability requests.
            try:
                await page.evaluate("""
                async () => {
                  for (let i = 0; i < 8; i++) {
                    window.scrollTo(0, document.body.scrollHeight);
                    await new Promise(r => setTimeout(r, 600));
                  }
                  window.scrollTo(0, 0);
                }
                """)
            except Exception:
                pass

            await asyncio.sleep(2)

            # Extract page-level evidence that may reveal direct endpoints.
            data = await page.evaluate("""
            () => ({
              links: [...document.querySelectorAll('a[href]')]
                .map(a => ({
                  text: (a.innerText || a.textContent || '').trim(),
                  href: a.href || ''
                }))
                .filter(x => /stock|qty|quantity|availability|available|stan|magazyn|inventory/i.test(x.text + ' ' + x.href))
                .slice(0,100),
              scripts: [...document.scripts]
                .map(s => s.src || s.textContent || '')
                .filter(x => /stock|qty|quantity|availability|available|stan|magazyn|inventory|ajax/i.test(x))
                .slice(0,40)
            })
            """)

            emit("network_page_hints", {
                "url": url,
                "links": data.get("links", []),
                "scripts": [str(x)[:8000] for x in data.get("scripts", [])],
            })

            # Extract product cards around Makanu availability icons.
            # s_duzo.png = in stock; s_brak.png = out of stock.
            if "/products/yak/odziez-kajakowa" in path:
                cards = await page.evaluate("""
                () => {
                  const icons = [...document.querySelectorAll('img[src*="s_duzo.png"], img[src*="s_brak.png"]')];

                  function clean(v) {
                    return (v || '').replace(/\\s+/g, ' ').trim();
                  }

                  function pickContainer(el) {
                    let cur = el;
                    for (let i = 0; i < 8 && cur; i++, cur = cur.parentElement) {
                      const links = cur.querySelectorAll ? cur.querySelectorAll('a[href]') : [];
                      const imgs = cur.querySelectorAll ? cur.querySelectorAll('img[src]') : [];
                      const txt = clean(cur.innerText || cur.textContent || '');
                      if (links.length >= 1 && imgs.length >= 1 && txt.length >= 8 && txt.length <= 2500) {
                        return cur;
                      }
                    }
                    return el.parentElement || el;
                  }

                  return icons.map(icon => {
                    const box = pickContainer(icon);
                    const links = [...box.querySelectorAll('a[href]')];
                    const productLink = links.find(a => /\\/product|\\/produkt|\\/p\\//i.test(a.getAttribute('href') || ''))
                      || links.find(a => clean(a.innerText || a.textContent).length > 2)
                      || links[0];

                    const allImgs = [...box.querySelectorAll('img[src]')]
                      .map(i => i.src)
                      .filter(src => src && !/s_duzo\\.png|s_brak\\.png/i.test(src));

                    const text = clean(box.innerText || box.textContent || '');
                    const href = productLink ? productLink.href : '';
                    const linkText = productLink ? clean(productLink.innerText || productLink.textContent || '') : '';
                    const iconSrc = icon.src || '';

                    return {
                      stock_status: /s_duzo\\.png/i.test(iconSrc) ? 'in_stock' : 'out_of_stock',
                      stock_qty: /s_duzo\\.png/i.test(iconSrc) ? 1 : 0,
                      availability_icon: iconSrc,
                      title: linkText || text.slice(0, 300),
                      text: text.slice(0, 1200),
                      source_url: href,
                      image_urls: [...new Set(allImgs)].slice(0, 20)
                    };
                  });
                }
                """)

                seen_cards = set()
                for card in cards:
                    key = (
                        card.get("source_url", ""),
                        card.get("title", ""),
                        card.get("availability_icon", ""),
                    )
                    if key in seen_cards:
                        continue
                    seen_cards.add(key)
                    emit("yak_product", {
                        "category": "YAK / odziez-kajakowa",
                        **card,
                    })

                emit("yak_category_summary", {
                    "url": url,
                    "products_found": len(seen_cards),
                    "in_stock": sum(1 for x in cards if x.get("stock_status") == "in_stock"),
                    "out_of_stock": sum(1 for x in cards if x.get("stock_status") == "out_of_stock"),
                })

        emit("crawl_summary", {
            "ok": True,
            "mode": "stock-network-discovery",
            "captured_candidates": len(captured),
            "unique_urls": sorted(set(x.get("url") for x in captured if x.get("url"))),
        })

    except Exception as exc:
        emit("crawl_fatal", {
            "ok": False,
            "mode": "stock-network-discovery",
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
