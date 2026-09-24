import os
from urllib.parse import urljoin, urlparse

from fastapi import FastAPI, HTTPException, Header
from playwright.async_api import async_playwright

app = FastAPI(title="Outfish Makanu Bridge", version="0.1.0")

BASE_URL = os.getenv("MAKANU_BASE_URL", "https://b2b.makanu.pl")
START_PATH = os.getenv("MAKANU_START_PATH", "/pulpit")
BRIDGE_API_KEY = os.getenv("BRIDGE_API_KEY", "")

def authorize(x_api_key: str | None):
    if not BRIDGE_API_KEY:
        raise HTTPException(status_code=503, detail="BRIDGE_API_KEY is not configured")
    if x_api_key != BRIDGE_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

@app.get("/")
async def root():
    return {"service": "outfish-makanu-bridge", "status": "running"}

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
                "has_password_field": await page.locator('input[type="password"]').count() > 0
            }
        finally:
            await browser.close()
