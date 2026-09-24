import asyncio
import os
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from fastapi import FastAPI, Header, HTTPException, Query
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

from crawl_catalog import main as crawl_catalog_main


app = FastAPI(
    title="Outfish Makanu Bridge",
    version="0.4.0",
)

BASE_URL = os.getenv(
    "MAKANU_BASE_URL",
    "https://b2b.makanu.pl",
)

START_PATH = os.getenv(
    "MAKANU_START_PATH",
    "/pulpit",
)

LOGIN = os.getenv("MAKANU_LOGIN", "")
PASSWORD = os.getenv("MAKANU_PASSWORD", "")
BRIDGE_API_KEY = os.getenv("BRIDGE_API_KEY", "")


# ---------------------------------------------------------
# AUTHORIZATION
# ---------------------------------------------------------

def authorize(x_api_key: str | None):
    if not BRIDGE_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="BRIDGE_API_KEY is not configured",
        )

    if x_api_key != BRIDGE_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
        )


# ---------------------------------------------------------
# MAKANU LOGIN
# ---------------------------------------------------------

async def open_logged_in_page():
    if not LOGIN:
        raise HTTPException(
            status_code=503,
            detail="MAKANU_LOGIN is not configured",
        )

    if not PASSWORD:
        raise HTTPException(
            status_code=503,
            detail="MAKANU_PASSWORD is not configured",
        )

    playwright = await async_playwright().start()
    browser = None
    context = None

    try:
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = await browser.new_context(
            viewport={
                "width": 1440,
                "height": 1000,
            },
            locale="pl-PL",
        )

        page = await context.new_page()

        await page.goto(
            urljoin(BASE_URL, START_PATH),
            wait_until="domcontentloaded",
            timeout=60000,
