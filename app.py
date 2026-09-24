import os
from urllib.parse import urljoin, urlparse

from fastapi import FastAPI, Header, HTTPException, Query
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

app = FastAPI(
    title="Outfish Makanu Bridge",
    version="0.3.0",
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
# AUTHORIZATION FOR OUR BRIDGE
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
        )

        # Give JS on the Makanu page a moment to initialize.
        try:
            await page.wait_for_load_state(
                "networkidle",
                timeout=10000,
            )
        except PlaywrightTimeoutError:
            pass

        # IMPORTANT:
        # Only use a VISIBLE password field.
        password_field = page.locator(
            'input[type="password"]:visible'
        ).first

        password_count = await password_field.count()

        # If there is no password field, we might already be logged in.
        if password_count == 0:
            return (
                playwright,
                browser,
                context,
                page,
            )

        # -------------------------------------------------
        # FIND LOGIN FIELD IN SAME FORM AS PASSWORD
        # -------------------------------------------------

        login_field = None

        form = password_field.locator(
            "xpath=ancestor::form[1]"
        )

        if await form.count():
            candidates = form.locator(
                'input:not([type="password"])'
                ':not([type="hidden"])'
                ':not([type="submit"])'
                ':not([type="button"])'
                ':not([type="checkbox"])'
                ':not([type="radio"])'
                ':visible'
            )

            candidate_count = await candidates.count()

            if candidate_count > 0:
                login_field = candidates.first

        # -------------------------------------------------
        # FALLBACK: FIND A VISIBLE LOGIN FIELD GLOBALLY
        # -------------------------------------------------

        if login_field is None:
            selectors = [
                'input[type="email"]:visible',
                'input[name*="login" i]:visible',
                'input[name*="user" i]:visible',
                'input[name*="email" i]:visible',
                'input[id*="login" i]:visible',
                'input[id*="user" i]:visible',
                'input[id*="email" i]:visible',
            ]

            for selector in selectors:
                candidate = page.locator(selector).first

                if await candidate.count():
                    login_field = candidate
                    break

        if login_field is None:
            await context.close()
            await browser.close()
            await playwright.stop()

            raise HTTPException(
                status_code=502,
                detail="Visible Makanu login field not found",
            )

        # -------------------------------------------------
        # FILL LOGIN + PASSWORD
        # -------------------------------------------------

        try:
            await login_field.fill(
                LOGIN,
                timeout=15000,
            )

            await password_field.fill(
                PASSWORD,
                timeout=15000,
            )

        except PlaywrightTimeoutError as exc:
            await context.close()
            await browser.close()
            await playwright.stop()

            raise HTTPException(
                status_code=502,
                detail=f"Could not fill Makanu login form: {exc}",
            )

        # -------------------------------------------------
        # SUBMIT LOGIN FORM
        # -------------------------------------------------

        submit = page.locator(
            'button[type="submit"]:visible, '
            'input[type="submit"]:visible'
        ).first

        try:
            if await submit.count():
                await submit.click(
                    timeout=15000,
                )
            else:
                await password_field.press("Enter")

        except PlaywrightTimeoutError as exc:
            await context.close()
            await browser.close()
            await playwright.stop()

            raise HTTPException(
                status_code=502,
                detail=f"Could not submit Makanu login form: {exc}",
            )

        # -------------------------------------------------
        # WAIT FOR LOGIN RESULT
        # -------------------------------------------------

        try:
            await page.wait_for_load_state(
                "networkidle",
                timeout=30000,
            )
        except PlaywrightTimeoutError:
            pass

        return (
            playwright,
            browser,
            context,
            page,
        )

    except HTTPException:
        raise

    except Exception as exc:
        try:
            await playwright.stop()
        except Exception:
            pass

        raise HTTPException(
            status_code=502,
            detail=f"Makanu browser error: {type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------
# ROOT
# ---------------------------------------------------------

@app.get("/")
async def root():
    return {
        "service": "outfish-makanu-bridge",
        "status": "running",
        "version": "0.3.0",
    }


# ---------------------------------------------------------
# HEALTH
# ---------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "outfish-makanu-bridge",
    }


# ---------------------------------------------------------
# PUBLIC MAKANU PAGE PROBE
# ---------------------------------------------------------

@app.get("/probe")
async def probe(
    x_api_key: str | None = Header(default=None),
):
    authorize(x_api_key)

    async with async_playwright() as playwright:
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
            }
        )

        page = await context.new_page()

        try:
            await page.goto(
                urljoin(BASE_URL, START_PATH),
                wait_until="domcontentloaded",
                timeout=60000,
            )

            try:
                await page.wait_for_load_state(
                    "networkidle",
                    timeout=10000,
                )
            except PlaywrightTimeoutError:
                pass

            visible_password_fields = await page.locator(
                'input[type="password"]:visible'
            ).count()

            return {
                "ok": True,
                "url": page.url,
                "title": await page.title(),
                "visible_password_fields": visible_password_fields,
            }

        finally:
            await context.close()
            await browser.close()


# ---------------------------------------------------------
# LOGIN FORM DIAGNOSTICS
# Does NOT return field values/password.
# ---------------------------------------------------------

@app.get("/login-form")
async def login_form(
    x_api_key: str | None = Header(default=None),
):
    authorize(x_api_key)

    async with async_playwright() as playwright:
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
            }
        )

        page = await context.new_page()

        try:
            await page.goto(
                urljoin(BASE_URL, START_PATH),
                wait_until="domcontentloaded",
                timeout=60000,
            )

            try:
                await page.wait_for_load_state(
                    "networkidle",
                    timeout=10000,
                )
            except PlaywrightTimeoutError:
                pass

            fields = await page.locator(
                "input:visible"
            ).evaluate_all(
                """
                els => els.map(el => ({
                    type: el.type || "",
                    name: el.name || "",
                    id: el.id || "",
                    placeholder: el.placeholder || "",
                    autocomplete: el.autocomplete || ""
                }))
                """
            )

            buttons = await page.locator(
                "button:visible, input[type='submit']:visible"
            ).evaluate_all(
                """
                els => els.map(el => ({
                    tag: el.tagName,
                    type: el.type || "",
                    name: el.name || "",
                    id: el.id || "",
                    text: (el.innerText || el.value || "").trim()
                }))
                """
            )

            return {
                "ok": True,
                "url": page.url,
                "title": await page.title(),
                "fields": fields,
                "buttons": buttons,
            }

        finally:
            await context.close()
            await browser.close()


# ---------------------------------------------------------
# LOGIN CHECK
# ---------------------------------------------------------

@app.get("/login-check")
async def login_check(
    x_api_key: str | None = Header(default=None),
):
    authorize(x_api_key)

    playwright = None
    browser = None
    context = None

    try:
        (
            playwright,
            browser,
            context,
            page,
        ) = await open_logged_in_page()

        visible_password_fields = await page.locator(
            'input[type="password"]:visible'
        ).count()

        return {
            "ok": True,
            "url": page.url,
            "title": await page.title(),
            "still_has_password_field": (
                visible_password_fields > 0
            ),
        }

    finally:
        if context:
            await context.close()

        if browser:
            await browser.close()

        if playwright:
            await playwright.stop()


# ---------------------------------------------------------
# READ AUTHENTICATED MAKANU PAGE
# ---------------------------------------------------------

@app.get("/page")
async def page_text(
    path: str = Query(default="/pulpit"),
    x_api_key: str | None = Header(default=None),
):
    authorize(x_api_key)

    parsed = urlparse(path)

    if (
        parsed.scheme
        or parsed.netloc
        or not path.startswith("/")
    ):
        raise HTTPException(
            status_code=400,
            detail="Only relative Makanu paths are allowed",
        )

    playwright = None
    browser = None
    context = None

    try:
        (
            playwright,
            browser,
            context,
            page,
        ) = await open_logged_in_page()

        target_url = urljoin(
            BASE_URL,
            path,
        )

        if page.url != target_url:
            await page.goto(
                target_url,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            try:
                await page.wait_for_load_state(
                    "networkidle",
                    timeout=15000,
                )
            except PlaywrightTimeoutError:
                pass

        body_text = await page.locator(
            "body"
        ).inner_text()

        links = await page.locator(
            "a"
        ).evaluate_all(
            """
            els => els.slice(0, 500).map(a => ({
                text: (a.innerText || "").trim(),
                href: a.href
            }))
            """
        )

        return {
            "ok": True,
            "url": page.url,
            "title": await page.title(),
            "text": body_text[:60000],
            "links": links,
        }

    finally:
        if context:
            await context.close()

        if browser:
            await browser.close()

        if playwright:
            await playwright.stop()
