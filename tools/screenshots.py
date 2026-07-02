"""Regenerate the screenshots in docs/images from a running dashboard.

    uv run --with playwright tools/screenshots.py "LVMH (MC) - Paris" "KERING (KER) - Paris"

The browser is installed once with `playwright install chromium`. Run the
dashboard without compose.override.yml, otherwise Dash draws its debug widget
over the bottom-right corner of every capture:

    docker compose -f compose.yml up -d

Pass the listings exactly as the selector spells them, "NAME (SYMBOL) - Market".
--chart and --scale set the controls before capturing.
"""

import argparse
import asyncio

from playwright.async_api import async_playwright

URL = "http://localhost:8050"
OUT = "docs/images"
TABS = [
    ("Prices", "prices"),
    ("Bollinger", "bollinger"),
    ("Raw data", "table"),
    ("Performance comparison", "performance"),
]


async def settle(page):
    """Wait for Dash to finish rendering: six years of points take a while."""
    await page.wait_for_function(
        "() => !document.querySelector('._dash-loading, .dash-spinner, .dash-loading')",
        timeout=180000,
    )
    await page.wait_for_timeout(4000)


async def main(stocks, chart, scale):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(
            viewport={"width": 1500, "height": 950}, device_scale_factor=2
        )
        page.set_default_timeout(180000)
        await page.goto(URL, wait_until="networkidle", timeout=60000)
        await page.wait_for_selector("#stock-selector")
        await page.wait_for_timeout(4000)

        await page.click("#stock-selector .dash-dropdown-trigger")
        await page.wait_for_timeout(1200)
        for name in stocks:
            await page.get_by_role("option", name=name, exact=True).first.click()
            await page.wait_for_timeout(500)
            print("selected", name)
        await page.keyboard.press("Escape")

        for label in (chart, scale):
            if label:
                await page.click(f"label:has-text('{label}')")
                await page.wait_for_timeout(500)
        await settle(page)

        for label, slug in TABS:
            await page.click(f".nav-link:has-text('{label}')")
            await settle(page)
            await page.screenshot(path=f"{OUT}/{slug}.png", full_page=True)
            print("captured", slug)

        await page.click(".nav-link:has-text('Prices')")
        await settle(page)
        await page.screenshot(path=f"{OUT}/overview.png")
        print("captured overview")
        await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stocks", nargs="+", help='e.g. "LVMH (MC) - Paris"')
    parser.add_argument("--chart", choices=["Line", "Candlestick"])
    parser.add_argument("--scale", choices=["Linear", "Logarithmic"])
    args = parser.parse_args()
    asyncio.run(main(args.stocks, args.chart, args.scale))
