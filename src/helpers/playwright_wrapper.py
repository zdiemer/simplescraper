from typing import Any

from playwright.async_api import async_playwright
from xvfbwrapper import Xvfb


async def request(url: str) -> Any:
    with Xvfb():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)
            page = await browser.new_page()
            await page.goto(url)
            content = await page.content()
            await browser.close()
            return content
