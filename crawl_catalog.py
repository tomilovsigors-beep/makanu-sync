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
