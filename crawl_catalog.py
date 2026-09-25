import asyncio
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from app import BASE_URL, open_logged_in_page

PAGE_TIMEOUT_MS = int(os.getenv("MAKANU_CRAWL_PAGE_TIMEOUT_MS", "60000"))
NETWORK_IDLE_TIMEOUT_MS = int(os.getenv("MAKANU_CRAWL_NETWORK_IDLE_TIMEOUT_MS", "7000"))
FS_PATH = "/p/StockDocument?symbol=FS"


def emit(event, payload):
    print(json.dumps({
        "event": event,
        "ts": datetime.now(timezone.utc).isoformat(),
        **payload
    }, ensure_ascii=False, separators=(",", ":")), flush=True)


def clean(v):
    return re.sub(r"\s+", " ", str(v or "")).strip()


def looks_like_sku(value):
    s = clean(value)
    if not s or len(s) > 50:
        return False
    if not re.search(r"[A-Za-z0-9]", s):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._/+\-]{3,50}", s))


def parse_numeric(value):
    s = clean(value)
    if not s:
        return None
    if not re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?", s):
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


async def main():
    playwright = browser = context = None

    try:
        playwright, browser, context, page = await open_logged_in_page()

        target = urljoin(BASE_URL, FS_PATH)
        await page.goto(
            target,
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

        emit("fs_page_loaded", {
            "url": page.url,
            "title": await page.title(),
        })

        diagnostics = await page.evaluate("""
        () => {
          const tables = [...document.querySelectorAll('table')].map((table, ti) => ({
            index: ti,
            headers: [...table.querySelectorAll('thead th, thead td')].map(x => (x.innerText || x.textContent || '').trim()),
            rows: [...table.querySelectorAll('tbody tr, tr')].slice(0, 5000).map((tr, ri) => ({
              index: ri,
              text: (tr.innerText || tr.textContent || '').trim(),
              cells: [...tr.querySelectorAll('th,td')].map(td => ({
                text: (td.innerText || td.textContent || '').trim(),
                html: td.innerHTML.slice(0,1500),
                attrs: Object.fromEntries([...td.attributes].map(a => [a.name,a.value]))
              })),
              attrs: Object.fromEntries([...tr.attributes].map(a => [a.name,a.value])),
              inputs: [...tr.querySelectorAll('input,select,button')].map(el => ({
                tag: el.tagName,
                type: el.type || '',
                name: el.name || '',
                id: el.id || '',
                value: el.value || '',
                text: (el.innerText || el.textContent || '').trim(),
                attrs: Object.fromEntries([...el.attributes].map(a => [a.name,a.value]))
              }))
            }))
          }));

          const links = [...document.querySelectorAll('a[href]')].map(a => ({
            text: (a.innerText || a.textContent || '').trim(),
            href: a.href || '',
            attrs: Object.fromEntries([...a.attributes].map(x => [x.name,x.value]))
          }));

          const scripts = [...document.scripts].map(s => ({
            src: s.src || '',
            text: (s.src ? '' : (s.textContent || '')).slice(0,50000)
          }));

          const forms = [...document.forms].map(f => ({
            action: f.action || '',
            method: (f.method || 'GET').toUpperCase(),
            html: f.outerHTML.slice(0,20000)
          }));

          const body = (document.body.innerText || '').slice(0,100000);

          return {tables, links, scripts, forms, body};
        }
        """)

        emit("fs_structure", {
            "table_count": len(diagnostics.get("tables", [])),
            "form_count": len(diagnostics.get("forms", [])),
            "body_excerpt": clean(diagnostics.get("body", ""))[:8000],
            "forms": diagnostics.get("forms", [])[:20],
        })

        row_candidates = []

        for table in diagnostics.get("tables", []):
            emit("fs_table_meta", {
                "table_index": table.get("index"),
                "headers": table.get("headers", []),
                "row_count": len(table.get("rows", [])),
            })

            for row in table.get("rows", []):
                cells = row.get("cells", [])
                texts = [clean(c.get("text")) for c in cells]
                nums = []
                skus = []

                for t in texts:
                    n = parse_numeric(t)
                    if n is not None:
                        nums.append(n)
                    if looks_like_sku(t):
                        skus.append(t)

                for inp in row.get("inputs", []):
                    v = clean(inp.get("value"))
                    if looks_like_sku(v):
                        skus.append(v)
                    n = parse_numeric(v)
                    if n is not None:
                        nums.append(n)

                attrs_blob = json.dumps(
                    {
                        "row_attrs": row.get("attrs", {}),
                        "inputs": row.get("inputs", []),
                        "cell_attrs": [c.get("attrs", {}) for c in cells],
                    },
                    ensure_ascii=False,
                )

                if (
                    skus
                    or nums
                    or any(k in attrs_blob.lower() for k in (
                        "stock","qty","quantity","available",
                        "availability","stan","magazyn","ilosc","ilość"
                    ))
                ):
                    candidate = {
                        "table_index": table.get("index"),
                        "row_index": row.get("index"),
                        "text": clean(row.get("text"))[:2000],
                        "cells": texts[:30],
                        "sku_candidates": sorted(set(skus))[:20],
                        "numeric_candidates": nums[:20],
                        "row_attrs": row.get("attrs", {}),
                        "inputs": row.get("inputs", [])[:30],
                    }
                    row_candidates.append(candidate)

        emit("fs_candidate_summary", {
            "candidate_rows": len(row_candidates),
        })

        for candidate in row_candidates[:500]:
            emit("fs_row_candidate", candidate)

        interesting_links = []
        for link in diagnostics.get("links", []):
            probe = f"{link.get('text','')} {link.get('href','')} {json.dumps(link.get('attrs',{}), ensure_ascii=False)}".lower()
            if any(k in probe for k in (
                "stock","qty","quantity","available","availability",
                "stan","magazyn","ilosc","ilość","json","ajax","api","export","csv","xml"
            )):
                interesting_links.append(link)

        emit("fs_interesting_links", {
            "count": len(interesting_links),
            "links": interesting_links[:100],
        })

        script_hits = []
        for script in diagnostics.get("scripts", []):
            probe = f"{script.get('src','')} {script.get('text','')}".lower()
            if any(k in probe for k in (
                "stock","qty","quantity","available","availability",
                "stan","magazyn","ilosc","ilość","ajax","json","api"
            )):
                script_hits.append({
                    "src": script.get("src"),
                    "text_excerpt": clean(script.get("text"))[:12000],
                })

        emit("fs_script_hits", {
            "count": len(script_hits),
            "scripts": script_hits[:30],
        })

        emit("crawl_summary", {
            "ok": True,
            "mode": "fs-html-parser",
            "candidate_rows": len(row_candidates),
            "table_count": len(diagnostics.get("tables", [])),
            "interesting_links": len(interesting_links),
            "script_hits": len(script_hits),
        })

    except Exception as exc:
        emit("crawl_fatal", {
            "ok": False,
            "mode": "fs-html-parser",
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
