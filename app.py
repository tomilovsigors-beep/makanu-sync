import os
from urllib.parse import urljoin, urlparse

from fastapi import FastAPI, Header, HTTPException, Query
from playwright.async_api import async_playwright

app = FastAPI(title="Outfish Makanu Bridge", version="0.2.0")

BASE_URL = os.getenv("MAKANU_BASE_URL", "https://b2b.makanu.pl")
START_PATH = os.getenv("MAKANU_START_PATH", "/pulpit")
LOGIN = os.getenv("MAKANU_LOGIN", "")
PASSWORD = os.getenv("MAKANU_PASSWORD", "")
BRIDGE_API_KEY = os.getenv("BRIDGE_API_KEY", "")

def authorize(x_api_key: str | None):
    if not BRIDGE_API_KEY:
        raise HTTPException(status_code=503, detail="BRIDGE_API_KEY is not configured")
    if x_api_key != BRIDGE_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

async def open_logged_in_page():
    if not LOGIN or not PASSWORD:
        raise HTTPException(status_code=503, detail="MAKANU_LOGIN or MAKANU_PASSWORD is not configured")

    p = await async_playwright().start()
    browser = await p.chromium.launch(headless=True)
    context = await browser.new_context()
    page = await context.new_page()

    await page.goto(urljoin(BASE_URL, START_PATH), wait_until="domcontentloaded", timeout=60000)

    password_field = page.locator('input[type="password"]').first
    if await password_field.count():
        form = password_field.locator("xpath=ancestor::form[1]")
login_field = form.locator(
    'input:not([type="password"]):not([type="hidden"]):not([type="submit"]):not([type="button"]):visible'
).first

        if not await login_field.count():
            login_field = page.locator(
                'input[type="email"]:visible, '
                'input[name*="login" i]:visible, '
                'input[name*="user" i]:visible, '
                'input[name*="email" i]:visible'
            ).first

        if not await login_field.count():
            await browser.close()
            await p.stop()
            raise HTTPException(status_code=502, detail="Makanu login field not found")

        await login_field.fill(LOGIN)
        await password_field.fill(PASSWORD)

        submit = page.locator('button[type="submit"], input[type="submit"]').first
        if await submit.count():
            await submit.click()
        else:
            await password_field.press("Enter")

        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass

    return p, browser, context, page

@app.get("/")
async def root():
    return {"service": "outfish-makanu-bridge", "status": "running", "version": "0.2.0"}

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/probe")
async def probe(x_api_key: str | None = Header(default=None)):
    authorize(x_api_key)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(urljoin(BASE_URL, START_PATH), wait_until="domcontentloaded", timeout=60000)
            return {
                "ok": True,
                "url": page.url,
                "title": await page.title(),
                "has_password_field": await page.locator('input[type="password"]').count() > 0,
            }
        finally:
            await browser.close()

@app.get("/login-check")
async def login_check(x_api_key: str | None = Header(default=None)):
    authorize(x_api_key)
    p, browser, context, page = await open_logged_in_page()
    try:
        return {
            "ok": True,
            "url": page.url,
            "title": await page.title(),
            "still_has_password_field": await page.locator('input[type="password"]').count() > 0,
        }
    finally:
        await context.close()
        await browser.close()
        await p.stop()

@app.get("/page")
async def page_text(
    path: str = Query("/pulpit"),
    x_api_key: str | None = Header(default=None),
):
    authorize(x_api_key)

    parsed = urlparse(path)
    if parsed.scheme or parsed.netloc or not path.startswith("/"):
        raise HTTPException(status_code=400, detail="Only relative Makanu paths are allowed")

    p, browser, context, page = await open_logged_in_page()
    try:
        await page.goto(urljoin(BASE_URL, path), wait_until="domcontentloaded", timeout=60000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        text = await page.locator("body").inner_text()
        links = await page.locator("a").evaluate_all(
            """els => els.slice(0, 250).map(a => ({text:(a.innerText||'').trim(), href:a.href}))"""
        )

        return {
            "ok": True,
            "url": page.url,
            "title": await page.title(),
            "text": text[:60000],
            "links": links,
        }
    finally:
        await context.close()
        await browser.close()
        await p.stop()
